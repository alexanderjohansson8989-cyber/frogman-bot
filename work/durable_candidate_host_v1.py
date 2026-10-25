"""Durable wrapper for the reviewed candidate. Injected model/source I/O only.

Private checkpoints precede every possible model dispatch. An interrupted owner
is retained for inspection; opening this host never retries an uncertain call.
"""
import contextlib
import datetime as dt
import copy
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from git_state_store_v1 import canonical, sha256, validate_files
from operations_ledger_v1 import Ledger, GlobalStop
from general_evaluation import instant

def database_bytes(ledger):
    target=sqlite3.connect(':memory:')
    try:
        ledger.db.backup(target)
        return target.serialize()
    finally:target.close()

def inspect_database(data,store):
    db=sqlite3.connect(':memory:')
    try:
        db.deserialize(data)
        if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise GlobalStop('Invalid SQLite snapshot')
        policy=db.execute('SELECT period,cap,stopped FROM policy WHERE singleton=1').fetchone()
        if policy is None or policy[:2]!=(store.period,store.cap) or policy[2] not in (0,1):
            raise GlobalStop('Database policy does not match manifest')
        # Reading the required tables also rejects a missing/incompatible schema.
        counts={name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                for name in ('operations','submissions','sessions')}
        blocked=(policy[2] or db.execute("SELECT COUNT(*) FROM operations WHERE state IN ('prepared','uncertain')").fetchone()[0]
                 or db.execute("SELECT COUNT(*) FROM submissions WHERE state IN ('prepared','uncertain')").fetchone()[0]
                 or db.execute("SELECT COUNT(*) FROM sessions WHERE status='active'").fetchone()[0])
        return dict(blocked=bool(blocked),counts=counts)
    except (sqlite3.Error,ValueError) as exc:raise GlobalStop('Unreadable SQLite snapshot') from exc
    finally:db.close()

def exact_json(value):
    if isinstance(value,Decimal):return str(value)
    if isinstance(value,dict):return {k:exact_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [exact_json(v) for v in value]
    return value

class CheckpointLedger(Ledger):
    def __init__(self,path,store,owner,clock):
        self.ready=False;self.store=store;self.owner=copy.deepcopy(owner);self.clock=clock
        self.archive={n:b for n,b in store.files.items() if n!='journal.sqlite3'}
        self.detail=None;self.batch_depth=0;self.batch_dirty=False
        self.staging=False;self.pending=False
        try:
            super().__init__(path,cap_usd=str(Decimal(store.cap)/1000000),period_key=store.period)
            self.db.execute('''CREATE TABLE IF NOT EXISTS checkpoint_events
                (sequence INTEGER PRIMARY KEY,created_utc TEXT NOT NULL,action TEXT NOT NULL,archive_sha256 TEXT NOT NULL)''')
            from checkpoint_objects_v1 import initialize
            initialize(self.db)
            self.ready=True
            self.record('host-open',{})
        except BaseException:
            if hasattr(self,'db'):self.db.close()
            raise

    def guard(self):
        if self.ready:self.store.assert_local_owner(self.owner)
        super().guard()

    @contextlib.contextmanager
    def checkpoint_batch(self):
        """One remote checkpoint for local transitions; NEVER wrap external I/O.

        Commit even on an exception: saved receipts and persistent stops must
        survive. The next external effect still needs a fresh ownership check.
        """
        if self.batch_depth:raise GlobalStop('Nested checkpoint batch forbidden')
        self.store.assert_local_owner(self.owner)
        self.batch_depth=1;self.batch_dirty=False
        try:yield
        finally:
            self.batch_depth=0
            if self.batch_dirty:self.checkpoint()

    @contextlib.contextmanager
    def tx(self):
        if self.ready:self.store.assert_local_owner(self.owner)
        with super().tx():
            yield
            if self.ready:
                action,arguments=self.detail or ('ledger-update',{})
                self._event(action,arguments)
        if self.ready:
            self.pending=True
            if self.batch_depth:self.batch_dirty=True
            elif not self.staging:self.checkpoint()

    def _event(self,action,arguments):
        now=self.clock().isoformat();instant(now)
        obj=dict(action=action,created_utc=now,arguments=exact_json(arguments))
        raw=canonical(obj);h=sha256(raw)
        from checkpoint_objects_v1 import pack
        root=pack(self.db,obj)
        cursor=self.db.execute('INSERT INTO checkpoint_events(created_utc,action,archive_sha256) VALUES(?,?,?)',(now,action,h))
        self.db.execute('INSERT INTO checkpoint_roots VALUES(?,?)',(cursor.lastrowid,root))

    def record(self,action,arguments):
        old=self.detail;self.detail=(action,arguments)
        try:
            with self.tx():pass
        finally:self.detail=old

    def stage_diagnostic(self,action,arguments):
        """Local durable audit only; next effect/accounting checkpoint flushes it.

        A hard process loss may lose these diagnostic-only remote updates.
        Session/operation/receipt/permit transitions are never deferred here.
        """
        if action not in ('pipeline-result','source-capture') or self.staging or self.batch_depth:
            raise GlobalStop('Only standalone diagnostic events may be staged')
        self.staging=True
        try:self.record(action,arguments)
        finally:self.staging=False

    def flush(self):
        if self.pending:self.checkpoint()

    def invoke(self,action,method,*args):
        old=self.detail;self.detail=(action,args)
        try:return method(*args)
        finally:self.detail=old

    def start_session(self,*args):return self.invoke('session-start',super().start_session,*args)
    def finish_session(self,*args):return self.invoke('session-finish',super().finish_session,*args)
    def reserve(self,*args):return self.invoke('operation-request',super().reserve,*args)
    def dispatch(self,*args):return self.invoke('operation-dispatch',super().dispatch,*args)
    def save_raw(self,*args):return self.invoke('operation-raw',super().save_raw,*args)
    def settle(self,*args):return self.invoke('operation-settle',super().settle,*args)
    def prepare_submission(self,*args):return self.invoke('submission-plan',super().prepare_submission,*args)
    def begin_submission(self,*args):return self.invoke('submission-dispatch',super().begin_submission,*args)
    def confirm_submission(self,*args):return self.invoke('submission-readback',super().confirm_submission,*args)

    def checkpoint(self):
        try:
            files=dict(self.archive,**{'journal.sqlite3':database_bytes(self)})
            validate_files(files)
            self.store.checkpoint(files,self.owner)
            self.pending=False
        except BaseException as exc:
            self.store.poisoned=True
            if isinstance(exc,GlobalStop):raise
            raise GlobalStop('Durable checkpoint failed') from exc


class DurableHost:
    def __init__(self,store,owner,folder,*,clock,credit_expires_utc,key_expires_utc,archive_finished=False):
        self.store,self.owner,self.clock=store,owner,clock
        self.expiry=min(instant(credit_expires_utc),instant(key_expires_utc))
        self.effect_deadline=self.expiry
        if type(archive_finished) is not bool:raise GlobalStop('Explicit archive mode required')
        self.archive_finished=archive_finished
        self.closed=False;self.ledger=None
        self.guard_time()
        _,files=store.load()
        if inspect_database(files['journal.sqlite3'],store)['blocked']:
            raise GlobalStop('Interrupted state requires inspection, not automatic restart')
        if store.manifest['owner'] is not None:raise GlobalStop('Existing owner must not be stolen')
        self.folder=Path(folder)
        self.folder.mkdir(parents=True,exist_ok=True)
        path=self.folder/'journal.sqlite3'
        if path.exists():raise GlobalStop('Restore requires a fresh local database path')
        # Claim before restoring a writable database; the winning commit fences
        # stale claimants. A later failure deliberately retains the remote owner.
        store.claim(owner)
        with path.open('xb') as f:f.write(files['journal.sqlite3'])
        self.ledger=CheckpointLedger(path,store,owner,clock)

    def guard_time(self):
        if self.clock()>=min(self.expiry,self.effect_deadline):raise GlobalStop('Question, credit or API-key period expired')

    def before_effect(self):
        if self.closed:raise GlobalStop('Host already closed')
        if self.ledger is not None and self.ledger.batch_depth:
            raise GlobalStop('External effect inside uncheckpointed batch forbidden')
        self.guard_time()
        if self.ledger is not None:self.ledger.flush()
        self.guard_time();self.store.assert_owner(self.owner)
        # Network ownership checks can themselves cross the deadline.
        self.guard_time()

    def guard_local(self):
        if self.closed:raise GlobalStop('Host already closed')
        self.guard_time();self.store.assert_local_owner(self.owner)

    def before_readback(self,deadline):
        """Bounded receipt reads after a possible write; no new write permit.

        Question cutoff limits dispatch, not reconciliation. Keep the stricter
        host expiry plus a separate short read window and remote ownership.
        """
        def local():
            if self.closed:raise GlobalStop('Host already closed')
            if self.clock()>=min(self.expiry,deadline):raise GlobalStop('Readback window expired')
            if self.ledger.batch_depth:raise GlobalStop('Readback inside checkpoint batch forbidden')
            self.store.assert_local_owner(self.owner)
        local();self.ledger.flush();self.store.assert_owner(self.owner);local()

    def model_transport(self,transport):
        def call(payload):
            self.before_effect()
            return transport(copy.deepcopy(payload))
        attest=getattr(transport,'checkpoint_dispatch_contract',None)
        if attest is not None:call.checkpoint_dispatch_contract=attest
        return call

    def save(self,result):
        self.ledger.stage_diagnostic('pipeline-result',result)

    def capture(self,callback):
        def call(*args):
            self.before_effect()
            result=callback(*args)
            folder=Path(args[-1]);names=[]
            for path in sorted(folder.rglob('*')):
                if path.is_symlink():raise GlobalStop('Source archive symlink forbidden')
                if not path.is_file():continue
                if path.suffix.lower() not in ('.json','.txt','.pdf','.html','.csv','.xml','.source'):
                    raise GlobalStop('Unexpected source archive file')
                data=path.read_bytes();h=sha256(data)
                self.ledger.archive['a-'+h+'.bin']=data
                names.append(dict(path=path.relative_to(folder).as_posix(),sha256=h,size=len(data)))
            self.ledger.stage_diagnostic('source-capture',dict(files=names,result=result))
            return result
        return call

    def run(self,binding,*,transport,count,capture_initial,capture_follow,candidates,publish=False):
        from candidate_service_v1 import run_bound_question
        self.effect_deadline=min(self.expiry,instant(binding['cutoff_utc']),self.clock()+dt.timedelta(hours=1))
        self.before_effect()
        from bounded_evidence import digest
        source_folder=self.folder/'sources'/digest(binding)
        result=run_bound_question(binding,source_folder,self.ledger,
            transport=self.model_transport(transport),count=self.model_transport(count),clock=self.clock,save=self.save,
            capture_initial=self.capture(capture_initial),capture_follow=self.capture(capture_follow),candidates=candidates,
            guard_host=self.guard_local,publish=publish)
        # Also publish terminal diagnostics for global stops when storage still
        # permits it. Financial transitions were already checkpointed separately.
        self.ledger.flush()
        if self.archive_finished and result['status']!='global_stop':
            from question_archive_v1 import compact_completed
            compact_completed(self)
        return result

    def close(self):
        if self.closed:return
        self.closed=True
        if self.ledger is None:return
        try:
            self.ledger.flush()
            # Prepared work may outlive its question (e.g. deadline after
            # submission preparation). Keep an owner for explicit recovery.
            active=self.ledger.db.execute("SELECT COUNT(*) FROM sessions WHERE status='active'").fetchone()[0]
            prepared=any(self.ledger.db.execute('SELECT 1 FROM '+table+" WHERE state='prepared'").fetchone()
                         for table in ('operations','submissions'))
            if not active and not prepared:
                self.ledger.guard()
                self.store.release(self.owner)
        finally:self.ledger.close()
