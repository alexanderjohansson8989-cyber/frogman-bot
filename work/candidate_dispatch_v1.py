"""Single-use journal-backed dispatch permits, not general API authorization."""
import json
from bounded_evidence import digest
from operations_ledger_v1 import GlobalStop

MODEL_CONTRACT='model-permit-before-http-v1'
SUBMISSION_CONTRACT='submission-permit-before-http-v1'

def fixed_submission_client(client,kind):
    from candidate_api_v1 import MetaculusClient,JSONClient
    method={'forecast':'forecast','comment':'create_comment'}.get(kind)
    return (method is not None and type(client) is MetaculusClient
            and type(client.http) is JSONClient
            and getattr(getattr(client,method),'__func__',None) is getattr(MetaculusClient,method)
            and getattr(client.http.call,'__func__',None) is JSONClient.call)

def record_submission_contract(ledger,key,plan,*,kind):
    """Before begin_submission only: bind the fixed permit-before-HTTP path.

    Absence of a permit is not evidence for legacy or arbitrary transports.
    Keep cancelled keys permanently; this contract never authorizes a retry.
    """
    if kind not in ('forecast','comment') or getattr(ledger,'effect_writes_allowed',True) is not True:
        raise GlobalStop('Invalid submission dispatch attestation')
    plan_sha=digest(plan);payload_sha=digest(plan['payload'])
    old=getattr(ledger,'detail',None)
    if hasattr(ledger,'detail'):ledger.detail=('submission-dispatch-contract',dict(key=key,kind=kind,plan_sha256=plan_sha,payload_sha256=payload_sha,protocol=SUBMISSION_CONTRACT))
    try:
        with ledger.tx():
            row=ledger.db.execute('SELECT plan_sha,plan,state,receipt FROM submissions WHERE key=?',(key,)).fetchone()
            if not row or row[0]!=plan_sha or digest(json.loads(row[1]))!=plan_sha or row[2:]!=('prepared',None):
                raise GlobalStop('Dispatch contract requires identical prepared submission')
            ledger.db.execute('''CREATE TABLE IF NOT EXISTS submission_dispatch_contracts
                (key TEXT PRIMARY KEY,kind TEXT NOT NULL,plan_sha256 TEXT NOT NULL,
                 payload_sha256 TEXT NOT NULL,protocol TEXT NOT NULL)''')
            ledger.db.execute('INSERT INTO submission_dispatch_contracts VALUES(?,?,?,?,?)',
                (key,kind,plan_sha,payload_sha,SUBMISSION_CONTRACT))
    finally:
        if hasattr(ledger,'detail'):ledger.detail=old

def record_model_contract(ledger,key,payload):
    """Attest the fixed client protocol in the reserve/dispatch checkpoint.

    No backfill: only a prepared operation can acquire this contract. Recovery
    may use absent permits only for operations explicitly attested here.
    """
    if getattr(ledger,'effect_writes_allowed',True) is not True:
        raise GlobalStop('Recovery cannot attest a new dispatch contract')
    old=getattr(ledger,'detail',None)
    if hasattr(ledger,'detail'):ledger.detail=('model-dispatch-contract',dict(key=key,protocol=MODEL_CONTRACT,payload_sha256=digest(payload)))
    try:
        with ledger.tx():
            row=ledger.db.execute('SELECT payload_sha,state,raw,actual,receipt_sha FROM operations WHERE key=?',(key,)).fetchone()
            if row!=(digest(payload),'prepared',None,None,None):raise GlobalStop('Dispatch contract requires new prepared operation')
            ledger.db.execute('''CREATE TABLE IF NOT EXISTS model_dispatch_contracts
                (key TEXT PRIMARY KEY,payload_sha256 TEXT NOT NULL,protocol TEXT NOT NULL)''')
            ledger.db.execute('INSERT INTO model_dispatch_contracts VALUES(?,?,?)',(key,digest(payload),MODEL_CONTRACT))
    finally:
        if hasattr(ledger,'detail'):ledger.detail=old

class DispatchPermit:
    def __init__(self,ledger,key,payload,*,kind,before_effect):
        if kind not in ('model','forecast','comment'):raise GlobalStop('Unknown effect kind')
        self.ledger,self.key,self.payload_sha=ledger,key,digest(payload)
        self.kind,self.before_effect,self.used=kind,before_effect,False
        self.http_entered=False
    def mark_http_entered(self,payload):
        """Volatile diagnostic only; persisted permits still govern recovery."""
        if not self.used or self.http_entered or digest(payload)!=self.payload_sha:
            raise GlobalStop('HTTP boundary requires matching unused transport entry')
        self.http_entered=True
    def __call__(self,payload):
        if self.used or digest(payload)!=self.payload_sha:raise GlobalStop('Dispatch permit used or payload changed')
        if getattr(self.ledger,'effect_writes_allowed',True) is not True:raise GlobalStop('Recovery cannot dispatch new writes')
        self.before_effect()
        db=self.ledger.db
        if self.kind=='model':
            row=db.execute('SELECT payload_sha,state,raw FROM operations WHERE key=?',(self.key,)).fetchone()
            if row!=(self.payload_sha,'uncertain',None):raise GlobalStop('No matching uncertain model dispatch')
        else:
            row=db.execute('SELECT plan,state FROM submissions WHERE key=?',(self.key,)).fetchone()
            if not row or row[1]!='uncertain':raise GlobalStop('No matching uncertain submission')
            plan=json.loads(row[0]);expected=plan['payload']
            if digest(expected)!=self.payload_sha:raise GlobalStop('Submission payload differs from journal')
        self.used=True
        old=getattr(self.ledger,'detail',None)
        if hasattr(self.ledger,'detail'):self.ledger.detail=('effect-permit',dict(key=self.key,kind=self.kind,payload_sha256=self.payload_sha))
        try:
            with self.ledger.tx():
                db.execute('''CREATE TABLE IF NOT EXISTS effect_permits
                    (key TEXT PRIMARY KEY,kind TEXT NOT NULL,payload_sha256 TEXT NOT NULL)''')
                if db.execute('SELECT 1 FROM effect_permits WHERE key=?',(self.kind+'/'+self.key,)).fetchone():
                    raise GlobalStop('This effect was already permitted; readback only')
                db.execute('INSERT INTO effect_permits VALUES(?,?,?)',(self.kind+'/'+self.key,self.kind,self.payload_sha))
        finally:
            if hasattr(self.ledger,'detail'):self.ledger.detail=old
        self.before_effect()

def model_transport(host,client):
    def send(payload):
        rows=host.ledger.db.execute("SELECT key FROM operations WHERE state='uncertain'").fetchall()
        if len(rows)!=1:raise GlobalStop('One active model dispatch required')
        permit=DispatchPermit(host.ledger,rows[0][0],payload,kind='model',before_effect=host.before_effect)
        return client.generate(payload,permit=permit)
    # Only the fixed, reviewed client path supplies absence-of-permit evidence.
    # Arbitrary injected transports keep the old conservative recovery behavior.
    from candidate_api_v1 import ClaudeClient,JSONClient
    if (type(client) is ClaudeClient and type(client.http) is JSONClient
            and getattr(client.generate,'__func__',None) is ClaudeClient.generate
            and getattr(client.http.call,'__func__',None) is JSONClient.call):
        def attest(ledger,key,payload):
            if ledger is not host.ledger:raise GlobalStop('Transport and journal differ')
            record_model_contract(ledger,key,payload)
        send.checkpoint_dispatch_contract=attest
    return send

def forecast_transport(host,client,plan):
    import datetime as dt
    from general_evaluation import instant
    b=plan['binding'];key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    host.effect_deadline=min(host.effect_deadline,instant(b['cutoff_utc']))
    read_deadline=min(host.expiry,host.clock()+dt.timedelta(seconds=90))
    def send(payload):
        return client.forecast(payload,permit=DispatchPermit(host.ledger,key,payload,kind='forecast',before_effect=host.before_effect))
    if fixed_submission_client(client,'forecast'):
        def attest(ledger,submission_key,submitted_plan):
            if ledger is not host.ledger or submission_key!=key or digest(submitted_plan)!=digest(plan):
                raise GlobalStop('Transport and submission journal differ')
            record_submission_contract(ledger,key,submitted_plan,kind='forecast')
        send.checkpoint_dispatch_contract=attest
    send.before_readback=lambda:host.before_readback(read_deadline)
    return send
