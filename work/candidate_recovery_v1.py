"""Explicit recovery: saved receipts and readback only, never a new POST."""
import json
import sqlite3
from pathlib import Path
from bounded_evidence import digest
from durable_candidate_host_v1 import CheckpointLedger,inspect_database,database_bytes
from operations_ledger_v1 import GlobalStop
from competition_adapter_v1 import reconcile

def inspect(store):
    manifest,files=store.load();state=inspect_database(files['journal.sqlite3'],store)
    db=sqlite3.connect(':memory:')
    try:
        db.deserialize(files['journal.sqlite3'])
        state.update(owner=manifest['owner'],head=store.head,
            operations=[dict(key=k,kind=t,state=s,raw_saved=r is not None) for k,t,s,r in db.execute('SELECT key,kind,state,raw FROM operations')],
            submissions=[dict(key=k,state=s) for k,s in db.execute('SELECT key,state FROM submissions')],
            sessions=[dict(key=k,status=s) for k,s in db.execute('SELECT key,status FROM sessions')])
        return state
    finally:db.close()

class RecoveryHost:
    def __init__(self,store,owner,folder,*,get_run,clock):
        self.store,self.owner=store,owner;self.closed=False
        _,files=store.load();inspect_database(files['journal.sqlite3'],store)
        old=store.manifest['owner']
        if old is None:raise GlobalStop('No interrupted owner to recover')
        terminal=get_run(old['run_id'])
        path=Path(folder)/'recovery.sqlite3';path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():raise GlobalStop('Recovery requires fresh local path')
        store.claim_recovery(owner,terminal)
        with path.open('xb') as f:f.write(files['journal.sqlite3'])
        self.ledger=CheckpointLedger(path,store,owner,clock)
        self.ledger.effect_writes_allowed=False
        self.ledger.record('recovery-owner',dict(previous_owner=old,completed_run_id=terminal['id'],run_attempt=terminal['run_attempt']))

    def settle_saved(self):
        """Only already persisted matching raw responses can release a reserve."""
        from prospective_runner_4 import cost
        from advisor_policy import OPUS,RATES
        from candidate_profile_v1 import OUT
        from decimal import Decimal
        self.store.assert_owner(self.owner);settled=[];unknown=[]
        for key,kind,text,reserved in self.ledger.db.execute("SELECT key,kind,raw,reserved FROM operations WHERE state='uncertain'").fetchall():
            if text is None or kind not in ('search','need','forecast','review'):
                unknown.append(key);continue
            try:raw=json.loads(text);amount=cost(raw,OPUS,kind)
            except Exception:unknown.append(key);continue
            # Ledger validates identical saved receipt hash and commits overrun
            # before stopping. This cannot manufacture or replace a raw answer.
            self.ledger.settle(key,amount,raw)
            if kind!='search':
                a,b=RATES[OPUS]
                counted_reserve=(Decimal(reserved)-Decimal(OUT[kind])*b)/a
                if counted_reserve<1 or counted_reserve!=counted_reserve.to_integral_value() or raw['usage']['input_tokens']>counted_reserve:
                    with self.ledger.tx():self.ledger.db.execute('UPDATE policy SET stopped=1')
                    raise GlobalStop('Recovered input exceeded preflight reserve; persistent stop')
            settled.append(key)
        return dict(settled=settled,unknown=unknown)

    def readback_forecasts(self,fetch):
        self.store.assert_owner(self.owner);verified=[];unknown=[]
        for key,plan in self.ledger.db.execute("SELECT key,plan FROM submissions WHERE state='uncertain'").fetchall():
            if json.loads(plan).get('version')=='candidate-private-comment-v1':continue
            try:receipt=reconcile(json.loads(plan),self.ledger,fetch=fetch)
            except Exception as exc:
                if self.store.poisoned:raise
                unknown.append(dict(key=key,error_type=type(exc).__name__));continue
            verified.append(dict(key=key,receipt=receipt))
        return dict(verified=verified,unknown=unknown)

    def readback_comments(self,client):
        from candidate_comments_v1 import readback,VERSION
        self.store.assert_owner(self.owner);verified=[];unknown=[]
        for key,text in self.ledger.db.execute("SELECT key,plan FROM submissions WHERE state='uncertain'").fetchall():
            plan=json.loads(text)
            if plan.get('version')!=VERSION:continue
            try:receipt=readback(plan,self.ledger,client)
            except Exception as exc:
                if self.store.poisoned:raise
                unknown.append(dict(key=key,error_type=type(exc).__name__));continue
            verified.append(dict(key=key,receipt=receipt))
        return dict(verified=verified,unknown=unknown)

    def abandon_finished_work(self):
        """End interrupted questions without ever regenerating that question."""
        self.store.assert_owner(self.owner)
        db=self.ledger.db
        if (db.execute("SELECT 1 FROM operations WHERE state='uncertain'").fetchone()
                or db.execute("SELECT 1 FROM submissions WHERE state='uncertain'").fetchone()
                or db.execute('SELECT stopped FROM policy').fetchone()[0]):
            raise GlobalStop('Unresolved operation or persistent stop remains')
        # A persisted prepared state proves dispatch was never checkpointed;
        # the transport protocol does not permit sending from this state.
        self.ledger.detail=('recovery-abandon',dict(reason='Owning workflow completed; no question is retried'))
        try:
            with self.ledger.tx():
                db.execute("UPDATE operations SET state='cancelled_unsent' WHERE state='prepared'")
                db.execute("UPDATE submissions SET state='cancelled_unsent' WHERE state='prepared'")
                db.execute("UPDATE sessions SET status='abandoned_recovered',result_sha=? WHERE status='active'",(digest(dict(status='abandoned_recovered')),))
        finally:self.ledger.detail=None

    def close(self):
        if self.closed:return
        self.closed=True
        try:
            state=inspect_database(database_bytes(self.ledger),self.store)
            if not state['blocked']:
                self.ledger.guard();self.store.release(self.owner)
        finally:self.ledger.close()
