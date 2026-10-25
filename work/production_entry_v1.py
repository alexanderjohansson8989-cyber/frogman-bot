"""Disabled preparation-only entrypoint. No bootstrap, scheduler or writes.

This version rejects submission configuration even when explicitly enabled.
Live model preparation still requires a current credit observation in run_cycle.
"""
import datetime as dt,json,os,secrets
from pathlib import Path
from bounded_evidence import digest
from candidate_api_v1 import ClaudeClient,MetaculusClient
from general_evaluation import instant
from git_state_store_v1 import SnapshotStore,owner_id
from github_git_transport_v1 import GitHubGit
from operations_ledger_v1 import GlobalStop,units
from production_adapters_v1 import list_posts,source_factory
from production_cycle_v1 import run_cycle,validate
from research_experiment_3 import candidates

REPO='alexanderjohansson8989-cyber/metaculus-bot'


def run(config,folder,*,git,mc,claude,run_id,attempt,code_sha256,clock,enabled=False):
    if enabled is not True:raise GlobalStop('Preparation entrypoint disabled')
    if config.get('submit') is not False or config.get('activation')!='prepare_only':
        raise GlobalStop('Entrypoint permits preparation only')
    return _run_core(config,folder,git=git,mc=mc,claude=claude,run_id=run_id,
        attempt=attempt,code_sha256=code_sha256,clock=clock)


def _run_core(config,folder,*,git,mc,claude,run_id,attempt,code_sha256,clock,before_intake=None):
    """Shared wiring; callers enforce their separate activation contracts."""
    if attempt!=1 or type(attempt) is not int:raise GlobalStop('No workflow rerun allowed')
    branch='frogman-state-production-v1' if config.get('submit') is True else 'frogman-state-preparation-v1'
    if config.get('state_branch')!=branch:raise GlobalStop('Explicit isolated state branch required')
    store=SnapshotStore(git,repository=REPO,branch=config['state_branch'],period=config['period'],
        cap_units=units(config['cap_usd']),code_sha256=code_sha256,clock=clock)
    deadline=validate(config,store,clock)
    def guard():
        if clock()>=deadline:raise GlobalStop('Preparation job expired')
        q=git.quota_observation()
        if (not instant(q['observed_utc'])<=clock()<instant(q['observed_utc'])+dt.timedelta(seconds=60)
                or q['remaining']<config['git_reserve'] or clock()>=instant(q['reset_utc'])):
            raise GlobalStop('Git quota stale or insufficient')
    # Existing private state only. Reading primes real header observations;
    # this must never silently initialize or replace a state branch.
    store.load();guard();owner=owner_id(run_id,attempt,secrets.token_hex(16))
    def listing_guard():
        guard();store.assert_owner(owner);guard()
    return run_cycle(config,store,owner,folder,
        mc=mc,claude=claude,listing=lambda tid:list_posts(mc,tid,guard=listing_guard),
        get_run=git.workflow_run,quota=git.quota_observation,clock=clock,
        capture_initial=None,capture_follow=None,capture_factory=source_factory(),
        candidates=candidates,enabled=True,before_intake=before_intake)


def main():
    # Check activation before reading credentials or making any request.
    if os.environ.get('FROGMAN_PREPARE_ENABLED')!='true':raise GlobalStop('Preparation entrypoint disabled')
    if os.environ.get('GITHUB_REPOSITORY')!=REPO:raise GlobalStop('Repository differs')
    config=json.loads(Path(os.environ['FROGMAN_INPUT']).read_bytes())
    if digest(config)!=os.environ['FROGMAN_INPUT_SHA']:raise GlobalStop('Frozen configuration changed')
    if config.get('submit') is not False:raise GlobalStop('Submission unavailable in this entrypoint')
    clock=lambda:dt.datetime.now(dt.timezone.utc)
    result=run(config,Path(os.environ['RUNNER_TEMP'])/'production-preparation',
        git=GitHubGit(REPO,os.environ['GITHUB_TOKEN'],clock=clock),
        mc=MetaculusClient(os.environ['METACULUS_TOKEN']),claude=ClaudeClient(os.environ['ANTHROPIC_API_KEY']),
        run_id=os.environ['GITHUB_RUN_ID'],attempt=int(os.environ['GITHUB_RUN_ATTEMPT']),
        code_sha256=os.environ['FROGMAN_BUNDLE_SHA'],clock=clock,enabled=True)
    # Queue summary has identities/status only, never full source/model text.
    print('PRODUCTION_PREPARATION_RESULT '+json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=='__main__':main()
