"""Durable, transactional budget and one-attempt operation journal (local disk).

The caller must persist this database between jobs. A GitHub hosted runner's
temporary disk is NOT a deployment persistence solution. No network here.
"""
import contextlib,json,sqlite3
from decimal import Decimal
from bounded_evidence import digest
from independent_runner import Stop

class GlobalStop(Stop):
    global_stop=True

@contextlib.contextmanager
def readback_scope(ledger,key):
    """Failed verification of an uncertain write is global; never clear or retry it."""
    try:
        yield
    except GlobalStop:
        raise
    except Exception as exc:
        try:
            row=ledger.db.execute('SELECT state FROM submissions WHERE key=?',(key,)).fetchone()
        except Exception:
            raise GlobalStop('Submission readback state unavailable; no retry') from None
        if row==('uncertain',):
            raise GlobalStop('Uncertain submission readback failed; no retry') from None
        raise

def units(value):
    if isinstance(value,(float,bool)):raise GlobalStop('Use exact decimal money')
    try:d=Decimal(value)
    except Exception as exc:raise GlobalStop('Invalid money') from exc
    if not d.is_finite() or d<0 or d*1000000!=(d*1000000).to_integral_value():raise GlobalStop('Invalid money precision')
    return int(d*1000000)

class Ledger:
    def __init__(self,path,*,cap_usd,period_key):
        cap=units(cap_usd)
        if not isinstance(period_key,str) or not period_key.strip() or cap<=0:raise GlobalStop('Budget period required')
        self.session_key=None
        self.db=sqlite3.connect(str(path),timeout=5,isolation_level=None)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1),period TEXT NOT NULL,cap INTEGER NOT NULL,stopped INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY,kind TEXT NOT NULL,payload_sha TEXT NOT NULL,reserved INTEGER NOT NULL,actual INTEGER,state TEXT NOT NULL,raw TEXT,receipt_sha TEXT);
        CREATE TABLE IF NOT EXISTS submissions (key TEXT PRIMARY KEY,plan_sha TEXT NOT NULL,plan TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT);
        CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY,contract_sha TEXT NOT NULL,status TEXT NOT NULL,result_sha TEXT);
        CREATE TABLE IF NOT EXISTS publications (key TEXT PRIMARY KEY,version TEXT NOT NULL,
            plan_sha TEXT NOT NULL,plan TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,
            forecast_verified INTEGER NOT NULL DEFAULT 0,comment_verified INTEGER NOT NULL DEFAULT 0);''')
        try:
            with self.tx():
                old=self.db.execute('SELECT period,cap FROM policy WHERE singleton=1').fetchone()
                if old is None:self.db.execute('INSERT INTO policy(singleton,period,cap) VALUES(1,?,?)',(period_key,cap))
                elif old!=(period_key,cap):raise GlobalStop('Stored budget policy changed; no automatic reset')
        except BaseException:self.db.close();raise
    @contextlib.contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:yield;self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise
    def close(self):self.db.close()
    def available_units(self):
        cap=self.db.execute('SELECT cap FROM policy').fetchone()[0]
        exposure=self.db.execute("SELECT COALESCE(SUM(CASE WHEN state='settled' THEN actual WHEN state IN ('prepared','uncertain') THEN reserved ELSE 0 END),0) FROM operations").fetchone()[0]
        return cap-exposure
    def guard(self):
        if self.db.execute('SELECT stopped FROM policy').fetchone()[0]:raise GlobalStop('Persistent stop is active')
        if self.db.execute("SELECT 1 FROM operations WHERE state='uncertain' LIMIT 1").fetchone():raise GlobalStop('Uncertain API operation requires reconciliation')
        if self.db.execute("SELECT 1 FROM submissions WHERE state='uncertain' LIMIT 1").fetchone():raise GlobalStop('Uncertain submission requires readback')
        active=self.db.execute("SELECT key FROM sessions WHERE status='active'").fetchall()
        if active and active!=[(self.session_key,)]:raise GlobalStop('Another or interrupted question session is active')
    def start_session(self,key,contract,minimum_usd):
        with self.tx():
            self.guard()
            if self.db.execute("SELECT 1 FROM publications WHERE state='pending'").fetchone():
                raise GlobalStop('Pending publication must finish before new research')
            if self.db.execute('SELECT 1 FROM sessions WHERE key=?',(key,)).fetchone():raise GlobalStop('Question session already attempted')
            if self.db.execute("SELECT 1 FROM sessions WHERE status='active'").fetchone():raise GlobalStop('Another question session is active')
            if units(minimum_usd)>self.available_units():raise GlobalStop('Insufficient budget for complete question')
            self.db.execute("INSERT INTO sessions(key,contract_sha,status) VALUES(?,?,'active')",(key,digest(contract)))
        self.session_key=key
    def finish_session(self,key,result,publish=False):
        if type(publish) is not bool:raise GlobalStop('Explicit publication intent required')
        with self.tx():
            self.guard()
            if self.session_key!=key:raise GlobalStop('Question session identity changed')
            if self.db.execute("SELECT status FROM sessions WHERE key=?",(key,)).fetchone()!=('active',):raise GlobalStop('Question session not active')
            if publish and result.get('status')=='prepared_local_only':
                # Same SQLite transaction AND durable checkpoint as completion.
                # Old dry-run rows never acquire publication authority on reopen.
                from publication_intent_v1 import register
                register(self,key,result['submission_plan'])
            self.db.execute("UPDATE sessions SET status='complete',result_sha=? WHERE key=?",(digest(result),key))
        self.session_key=None
    def reserve(self,key,kind,payload,reserve_usd):
        amount=units(reserve_usd)
        if not amount or not key:raise GlobalStop('Positive operation reservation required')
        with self.tx():
            self.guard()
            if self.db.execute('SELECT 1 FROM operations WHERE key=?',(key,)).fetchone():raise GlobalStop('Operation already attempted')
            if amount>self.available_units():raise GlobalStop('Persistent budget exhausted')
            self.db.execute('INSERT INTO operations(key,kind,payload_sha,reserved,state) VALUES(?,?,?,?,?)',(key,kind,digest(payload),amount,'prepared'))
    def dispatch(self,key,payload):
        with self.tx():
            self.guard()
            row=self.db.execute('SELECT payload_sha,state FROM operations WHERE key=?',(key,)).fetchone()
            if row!=(digest(payload),'prepared'):raise GlobalStop('Operation identity/state changed')
            self.db.execute("UPDATE operations SET state='uncertain' WHERE key=?",(key,))
    def save_raw(self,key,raw):
        text=json.dumps(raw,ensure_ascii=False,allow_nan=False)
        with self.tx():
            row=self.db.execute('SELECT raw,state FROM operations WHERE key=?',(key,)).fetchone()
            if not row or row[1]!='uncertain' or (row[0] is not None and row[0]!=text):raise GlobalStop('Raw receipt identity/state changed')
            self.db.execute('UPDATE operations SET raw=?,receipt_sha=? WHERE key=?',(text,digest(raw),key))
    def settle(self,key,actual_usd,raw):
        amount=units(actual_usd);over=False
        with self.tx():
            row=self.db.execute('SELECT reserved,receipt_sha,state FROM operations WHERE key=?',(key,)).fetchone()
            if not row or row[1]!=digest(raw) or row[2]!='uncertain':raise GlobalStop('Cannot settle without identical saved receipt')
            self.db.execute("UPDATE operations SET actual=?,state='settled' WHERE key=?",(amount,key))
            over=amount>row[0] or self.available_units()<0
            if over:self.db.execute('UPDATE policy SET stopped=1')
        if over:raise GlobalStop('Observed cost exceeded reservation; persistent stop')
    def prepare_submission(self,key,plan):
        with self.tx():
            self.guard()
            if self.db.execute('SELECT 1 FROM submissions WHERE key=?',(key,)).fetchone():raise GlobalStop('Submission already registered')
            self.db.execute('INSERT INTO submissions VALUES(?,?,?,?,NULL)',(key,digest(plan),json.dumps(plan,ensure_ascii=False,allow_nan=False),'prepared'))
    def begin_submission(self,key,plan):
        with self.tx():
            self.guard()
            if self.db.execute("SELECT 1 FROM submissions WHERE state='uncertain'").fetchone():raise GlobalStop('Uncertain submission requires readback')
            row=self.db.execute('SELECT plan_sha,state FROM submissions WHERE key=?',(key,)).fetchone()
            if row!=(digest(plan),'prepared'):raise GlobalStop('Submission identity/state changed')
            self.db.execute("UPDATE submissions SET state='uncertain' WHERE key=?",(key,))
    def confirm_submission(self,key,plan,receipt):
        with self.tx():
            row=self.db.execute('SELECT plan_sha,state FROM submissions WHERE key=?',(key,)).fetchone()
            if row!=(digest(plan),'uncertain'):raise GlobalStop('Submission cannot be confirmed')
            self.db.execute("UPDATE submissions SET state='verified',receipt=? WHERE key=?",(json.dumps(receipt,ensure_ascii=False,allow_nan=False),key))

class ReservedRequester:
    """Exactly one search, need, forecast and review per new question run."""
    def __init__(self,ledger,run_key,*,transport,count,guard):
        self.ledger=ledger;self.run_key=run_key;self.transport=transport;self.count=count;self.guard=guard;self.slots={}
    def __call__(self,stage,payload):
        from candidate_profile_v1 import OUT,EFFORT,STAGE_LIMITS
        from advisor_policy import OPUS,RATES
        from candidate_cost_v1 import cost
        limits=STAGE_LIMITS
        self.guard();self.ledger.guard()
        slot=self.slots.get(stage,0)
        if stage not in limits or slot>=limits[stage]:raise GlobalStop('Stage slot exhausted')
        p=json.loads(json.dumps(payload,allow_nan=False))
        expected={'model','system','messages','max_tokens','output_config'}|({'tools'} if stage=='search' else set())
        if (set(p)!=expected or p['model']!=OPUS or p['max_tokens']!=OUT[stage]
            or p['output_config']!={'effort':EFFORT[stage]}
            or not isinstance(p['system'],str) or not isinstance(p['messages'],list) or not p['messages']
            or any(set(m)!={'role','content'} or m['role']!='user' or not isinstance(m['content'],str) for m in p['messages'])):raise GlobalStop('Unreviewed candidate request')
        if stage=='search' and p['tools']!=[dict(type='web_search_20250305',name='web_search',max_uses=2)]:raise GlobalStop('Unreviewed search tools')
        n=self.count(p)
        self.guard()
        if type(n) is not int or not 1<=n<=30000:raise GlobalStop('Full input limit')
        a,b=RATES[OPUS];reserve=Decimal('1.20') if stage=='search' else (Decimal((n*120+99)//100)*a+Decimal(OUT[stage])*b)/1000000
        key=self.run_key+'/'+stage+'/'+str(slot)
        # These are local state transitions only. Durable ledgers combine their
        # remote publication; a plain local ledger retains its normal behavior.
        batch=getattr(self.ledger,'checkpoint_batch',contextlib.nullcontext)
        with batch():
            self.ledger.reserve(key,stage,p,reserve)
            attest=getattr(self.transport,'checkpoint_dispatch_contract',None)
            if attest is not None:attest(self.ledger,key,p)
            self.ledger.dispatch(key,p)
        self.slots[stage]=slot+1
        # The durable checkpoint can block on disk/remote persistence. Recheck
        # immediately before transport; retain uncertain state if time expired.
        self.guard()
        try:raw=self.transport(p)
        except Exception as exc:raise GlobalStop('Transport unknown; reservation retained') from exc
        with batch():
            self.ledger.save_raw(key,raw)
            try:amount=cost(raw,OPUS,stage)
            except Exception as exc:raise GlobalStop('Usage unknown; reservation retained') from exc
            self.ledger.settle(key,amount,raw)
            if raw['usage']['output_tokens']>OUT[stage]:
                with self.ledger.tx():self.ledger.db.execute('UPDATE policy SET stopped=1')
                raise GlobalStop('Output exceeded candidate request limit; persistent stop')
        self.guard()
        if stage!='search' and raw['usage']['input_tokens']>(n*120+99)//100:
            with self.ledger.tx():self.ledger.db.execute('UPDATE policy SET stopped=1')
            raise GlobalStop('Input exceeded counted reserve')
        return raw
