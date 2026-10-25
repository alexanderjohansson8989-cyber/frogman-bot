"""Compact completed data only after pinning its verified ancestor snapshot.

Git commits remain reachable through the one-parent state branch. No force
pushes, new branch, budget reset, model effects or unresolved-work cleanup.
"""
import json
import sqlite3
from git_state_store_v1 import canonical,sha256
from operations_ledger_v1 import GlobalStop
from checkpoint_objects_v1 import events_from_snapshot

VERSION='frogman-question-archive-v1'

def session_result(events,key,status):
    if status=='complete':
        matches=[e['arguments'][1] for e in events if e['action']=='session-finish' and e['arguments'][0]==key]
    elif status=='abandoned_recovered':
        matches=[e['arguments']['result'] for e in events if e['action']=='recovery-abandon'
                 and key in e['arguments'].get('sessions',[]) and e['arguments'].get('result')==dict(status=status)]
    else:raise GlobalStop('Nonterminal archive session')
    if len(matches)!=1:raise GlobalStop('Archived terminal result missing or ambiguous')
    return matches[0]


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS question_archives (
        session_key TEXT PRIMARY KEY, contract_sha TEXT NOT NULL,
        result_sha TEXT NOT NULL, commit_sha TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL, version TEXT NOT NULL)''')


def compact_completed(host):
    ledger=host.ledger;store=host.store;db=ledger.db
    if host.closed:raise GlobalStop('Closed host cannot archive')
    store.assert_owner(host.owner);ledger.guard()
    if ledger.batch_depth:raise GlobalStop('Cannot archive an unfinished checkpoint batch')
    if db.execute("SELECT 1 FROM publications WHERE state='pending' LIMIT 1").fetchone():
        raise GlobalStop('Pending publication cannot be archived')
    if db.execute("SELECT 1 FROM sessions WHERE status NOT IN ('complete','abandoned_recovered') LIMIT 1").fetchone():
        raise GlobalStop('Only completed sessions may be archived')
    if db.execute("SELECT 1 FROM operations WHERE state NOT IN ('settled','cancelled_unsent') LIMIT 1").fetchone():
        raise GlobalStop('Unresolved operation cannot be archived')
    if db.execute("SELECT 1 FROM submissions WHERE state NOT IN ('verified','cancelled_unsent') LIMIT 1").fetchone():
        raise GlobalStop('Unresolved submission cannot be archived')
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='question_archives' AND type='table'").fetchone()
    old_keys={x[0] for x in db.execute('SELECT session_key FROM question_archives')} if exists else set()
    rows=[r for r in db.execute('SELECT key,contract_sha,result_sha FROM sessions') if r[0] not in old_keys]
    if not rows:return None
    if any(not r[2] for r in rows):raise GlobalStop('Completed session lacks result identity')
    # No content is removed until the remote ancestor has been verified. The
    # current in-memory data must equal that committed snapshot as well.
    from durable_candidate_host_v1 import database_bytes
    if database_bytes(ledger)!=store.files['journal.sqlite3']:
        raise GlobalStop('Archive requires fully checkpointed database')
    ancestor=store.head;manifest,files=store._read(ancestor)
    if manifest!=store.manifest or files!=store.files:
        raise GlobalStop('Archive snapshot changed')
    events=events_from_snapshot(files)  # validates every full event before compaction
    from bounded_evidence import digest
    for key,contract,result_sha in rows:
        status=db.execute('SELECT status FROM sessions WHERE key=?',(key,)).fetchone()[0]
        if digest(session_result(events,key,status))!=result_sha:
            raise GlobalStop('Terminal result does not match archive session')
    manifest_sha=sha256(canonical(manifest))
    prior_archive=dict(ledger.archive)
    try:
        # Batch means a single remote checkpoint after local compaction. It
        # still flushes a committed local change if a later step raises.
        with ledger.checkpoint_batch():
            old_detail=ledger.detail
            ledger.detail=('question-archive',dict(version=VERSION,commit_sha=ancestor,
                manifest_sha256=manifest_sha,sessions=[r[0] for r in rows]))
            try:
                with ledger.tx():
                    schema(db)
                    for key,contract,result in rows:
                        db.execute('INSERT INTO question_archives VALUES(?,?,?,?,?,?)',
                            (key,contract,result,ancestor,manifest_sha,VERSION))
                    # Compact payloads only. Every accounting, identity, state
                    # and consumed-permit row stays in the active journal.
                    db.execute('UPDATE operations SET raw=NULL WHERE state=\'settled\'')
                    # Pending plans are prohibited above. Terminal full plans
                    # remain in the verified ancestor; keep identity and outcome.
                    db.execute("UPDATE publications SET plan='' WHERE state IN ('complete','not_submitted','forecast_only')")
                    db.execute('DELETE FROM checkpoint_roots')
                    db.execute('DELETE FROM checkpoint_events')
                    db.execute('DELETE FROM checkpoint_objects')
            finally:ledger.detail=old_detail
            ledger.archive={}
            db.execute('VACUUM')
    except BaseException:
        # A failed remote publication never authorizes another external action.
        ledger.archive=prior_archive;store.poisoned=True
        raise
    return dict(commit_sha=ancestor,manifest_sha256=manifest_sha,sessions=[r[0] for r in rows])


def load_archived_session(store, active_db, session_key):
    """Read-only restore against the immutable pointer in the active journal."""
    row=active_db.execute('SELECT contract_sha,result_sha,commit_sha,manifest_sha256,version FROM question_archives WHERE session_key=?',(session_key,)).fetchone()
    if not row or row[4]!=VERSION:raise GlobalStop('Unknown archived session')
    current=active_db.execute('SELECT contract_sha,result_sha,status FROM sessions WHERE key=?',(session_key,)).fetchone()
    if current is None or current[:2]!=row[:2] or current[2] not in ('complete','abandoned_recovered'):
        raise GlobalStop('Archive/session identity changed')
    manifest,files=store._read(row[2])
    if sha256(canonical(manifest))!=row[3]:raise GlobalStop('Archived manifest identity changed')
    db=sqlite3.connect(':memory:')
    try:
        db.deserialize(files['journal.sqlite3'])
        if db.execute('SELECT contract_sha,result_sha,status FROM sessions WHERE key=?',(session_key,)).fetchone()!=current:
            raise GlobalStop('Archived question identity changed')
        events=events_from_snapshot(files)
        result=session_result(events,session_key,current[2])
        from bounded_evidence import digest
        if digest(result)!=row[1]:
            raise GlobalStop('Archived result identity changed')
        return dict(result=result,events=events,files=files,manifest=manifest)
    finally:db.close()
