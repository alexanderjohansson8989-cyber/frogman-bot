"""One audited replacement of a failed diagnostic with no dispatched effects.

The old branch is preserved. Any changed head, non-terminal owner or even one
operation/submission blocks replacement. This is not general retry/recovery.
"""
import sqlite3
from git_state_store_v1 import SnapshotStore
from durable_candidate_host_v1 import inspect_database
from operations_ledger_v1 import GlobalStop

REPO='alexanderjohansson8989-cyber/metaculus-bot'
OLD_BRANCH='frogman-state-fourtype-dryrun-v2'
OLD_HEAD='ad835f8ed7128d4f9f7dbcfa3475f0f7804cab1a'
OLD_CODE='ea5a1f0e2c2231443bb06a2ff0470884b442c9568f286a6f6ab5415481a97407'
OLD_RUN='39054852239'

def verify_zero_effects(git,clock):
    run=git.workflow_run(OLD_RUN)
    if (run.get('id')!=int(OLD_RUN) or run.get('run_attempt')!=1 or run.get('status')!='completed'
            or run.get('conclusion')!='failure' or run.get('repository',{}).get('full_name')!=REPO):
        raise GlobalStop('Original failed diagnostic must be terminal')
    store=SnapshotStore(git,repository=REPO,branch=OLD_BRANCH,period='fourtype-dryrun-v2',
        cap_units=9000000,code_sha256=OLD_CODE,clock=clock)
    manifest,files=store.load()
    if store.head!=OLD_HEAD or manifest['owner']['run_id']!=OLD_RUN or manifest['sequence']!=5:
        raise GlobalStop('Original diagnostic state changed')
    inspected=inspect_database(files['journal.sqlite3'],store)
    if inspected['counts']!={'operations':0,'submissions':0,'sessions':1}:
        raise GlobalStop('Replacement forbids any existing API operation')
    db=sqlite3.connect(':memory:')
    try:
        db.deserialize(files['journal.sqlite3'])
        actions=[r[0] for r in db.execute('SELECT action FROM checkpoint_events ORDER BY sequence')]
        if actions!=['host-open','frozen-fourtype-input','session-start','pipeline-result']:
            raise GlobalStop('Original diagnostic event sequence changed')
    finally:db.close()
    if git.head(OLD_BRANCH)!=OLD_HEAD:raise GlobalStop('Original diagnostic changed during inspection')
    return dict(old_run_id=OLD_RUN,old_head=OLD_HEAD,old_branch=OLD_BRANCH,operations=0,submissions=0,
        actual_cost_units=0,policy='single replacement, shared original nine dollar cap')
