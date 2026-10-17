"""Single-use journal-backed dispatch permits, not general API authorization."""
import json
from bounded_evidence import digest
from operations_ledger_v1 import GlobalStop

class DispatchPermit:
    def __init__(self,ledger,key,payload,*,kind,before_effect):
        if kind not in ('model','forecast','comment'):raise GlobalStop('Unknown effect kind')
        self.ledger,self.key,self.payload_sha=ledger,key,digest(payload)
        self.kind,self.before_effect,self.used=kind,before_effect,False
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
    return send

def forecast_transport(host,client,plan):
    from general_evaluation import instant
    b=plan['binding'];key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    host.effect_deadline=min(host.effect_deadline,instant(b['cutoff_utc']))
    def send(payload):
        return client.forecast(payload,permit=DispatchPermit(host.ledger,key,payload,kind='forecast',before_effect=host.before_effect))
    return send
