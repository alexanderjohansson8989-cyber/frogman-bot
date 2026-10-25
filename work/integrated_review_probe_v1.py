"""Single-use integration check after storage, recovery and classified review fixes.

This is a separately capped diagnostic, not recovery or a zero-cost retry.
The previous paid journal stays immutable and its cost is recorded as prior cost.
"""
import datetime as dt,json,os,sqlite3
from pathlib import Path
from fourtype_dryrun_v2 import run,REPO,ReadOnlyMetaculus,TimedOpener
from candidate_api_v1 import ClaudeClient
from github_git_transport_v1 import GitHubGit
from git_state_store_v1 import SnapshotStore
from durable_candidate_host_v1 import inspect_database
from operations_ledger_v1 import GlobalStop
from bounded_evidence import digest

BRANCH='frogman-state-integrated-review-20261019'
PERIOD='integrated-review-20261019'
OLD_BRANCH='frogman-state-fourtype-parser-20261018'
OLD_HEAD='8e63d27b4326d4f1c167a6f69e18af793220fd52'
OLD_CODE='1f5be19a4c269e9a21b15d0a3133594aa6ab21ae1faf1354332aaf87a4a9892b'
OLD_RUN='39106719406'

def verify_completed(git,clock):
    prior=git.workflow_run(OLD_RUN)
    if (prior.get('id')!=int(OLD_RUN) or prior.get('run_attempt')!=1 or prior.get('status')!='completed'
            or prior.get('conclusion')!='success' or prior.get('repository',{}).get('full_name')!=REPO):
        raise GlobalStop('Previous paid diagnostic must be complete')
    store=SnapshotStore(git,repository=REPO,branch=OLD_BRANCH,period='fourtype-parser-20261018',
        cap_units=9000000,code_sha256=OLD_CODE,clock=clock)
    manifest,files=store.load()
    if store.head!=OLD_HEAD or manifest['owner'] is not None or manifest['sequence']!=165:
        raise GlobalStop('Previous paid diagnostic state changed')
    check=inspect_database(files['journal.sqlite3'],store)
    if check['blocked'] or check['counts']!={'operations':20,'submissions':0,'sessions':4}:
        raise GlobalStop('Previous diagnostic has unresolved effects')
    db=sqlite3.connect(':memory:')
    try:
        db.deserialize(files['journal.sqlite3'])
        if db.execute("SELECT COUNT(*) FROM operations WHERE state!='settled'").fetchone()[0]:
            raise GlobalStop('Previous operation is not settled')
        cost=db.execute('SELECT SUM(actual) FROM operations').fetchone()[0]
        if cost!=2114560:raise GlobalStop('Previous paid cost changed')
    finally:db.close()
    if git.head(OLD_BRANCH)!=OLD_HEAD:raise GlobalStop('Previous state changed during inspection')
    return dict(old_run_id=OLD_RUN,old_head=OLD_HEAD,old_branch=OLD_BRANCH,prior_actual_cost_units=cost,
        policy='bounded integration check; separate single-use nine dollar experiment; prior cost retained')

def main():
    if os.environ.get('GITHUB_REPOSITORY')!=REPO or os.environ.get('GITHUB_RUN_ATTEMPT')!='1':
        raise GlobalStop('First run in fixed repository only')
    p=json.loads(Path(os.environ['FROGMAN_INPUT']).read_bytes())
    if digest(p)!=os.environ['FROGMAN_INPUT_SHA'] or p.get('experiment')!=PERIOD:
        raise GlobalStop('Frozen parser experiment input changed')
    events=[]
    return run(p,GitHubGit(REPO,os.environ['GITHUB_TOKEN']),ReadOnlyMetaculus(os.environ['METACULUS_TOKEN']),
        ClaudeClient(os.environ['ANTHROPIC_API_KEY'],opener=TimedOpener(events)),
        Path(os.environ['RUNNER_TEMP'])/'integrated-review-run',run_id=os.environ['GITHUB_RUN_ID'],attempt=1,
        code_sha256=os.environ['FROGMAN_BUNDLE_SHA'],clock=lambda:dt.datetime.now(dt.timezone.utc),events=events,
        verify_previous=verify_completed,state_branch=BRANCH,state_period=PERIOD,prior_label='previous_paid_diagnostic',archive_finished=True)

if __name__=='__main__':main()
