"""Prepared one-shot entrypoint. Requires exact approval; never run by CI.

Fresh fixed private state branch prevents a second attempt after any interruption.
No Anthropic client/credential, no models, no production state or schedule.
"""
import datetime as dt
import json
import os
import secrets
from pathlib import Path
from acceptance_runtime_v1 import execute
from candidate_api_v1 import MetaculusClient
from durable_candidate_host_v1 import DurableHost,database_bytes
from git_state_store_v1 import SnapshotStore,owner_id
from github_git_transport_v1 import GitHubGit
from operations_ledger_v1 import Ledger,GlobalStop
from test_area_acceptance_v1 import check_plan
from general_evaluation import instant

REPO='alexanderjohansson8989-cyber/metaculus-bot'
BRANCH='frogman-state-acceptance-first-v1'
PERIOD='unscored-acceptance-no-models-v1'


def run(plan,git,client,folder,*,approved_plan_sha256,run_id,attempt,code_sha256,clock):
    check_plan(plan)
    if approved_plan_sha256!=plan['plan_sha256'] or attempt!=1:
        raise GlobalStop('Approved exact plan and first workflow attempt required')
    if not instant(plan['binding']['bound_at_utc'])<=clock()<instant(plan['valid_until_utc']):
        raise GlobalStop('Acceptance plan expired or clock reversed')
    dest=Path(folder);dest.mkdir(parents=True,exist_ok=False)
    # Existing Ledger schema requires a positive cap. One inert unit is used;
    # this entrypoint never creates an operation or a financial reservation.
    store=SnapshotStore(git,repository=REPO,branch=BRANCH,period=PERIOD,cap_units=1,
                        code_sha256=code_sha256,clock=clock)
    initial=Ledger(dest/'initial.sqlite3',cap_usd='0.000001',period_key=PERIOD)
    try:store.bootstrap({'journal.sqlite3':database_bytes(initial)})
    finally:initial.close()
    # Both generic expiry fields are bounded by this test's authorization
    # lifetime. They do not assert a real provider credit or API-key expiry.
    host=DurableHost(store,owner_id(run_id,attempt,secrets.token_hex(16)),dest/'restored',clock=clock,
        credit_expires_utc=plan['valid_until_utc'],key_expires_utc=plan['valid_until_utc'])
    result=dict(status='stopped',plan_sha256=plan['plan_sha256'],claude_calls=0,state_branch=BRANCH)
    try:
        result.update(execute(plan,host,client,approved_plan_sha256=approved_plan_sha256))
        return result
    except BaseException as exc:
        result.update(error_type=type(exc).__name__,reason=str(exc)[:180] if isinstance(exc,GlobalStop) else 'Acceptance error; inspect journal')
        raise
    finally:
        try:host.close()
        finally:
            result.update(state_head=store.head,owner_released=store.manifest['owner'] is None)
            (dest/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
            print('ACCEPTANCE_WRITE_RESULT '+json.dumps(result,sort_keys=True))


def main():
    if os.environ.get('GITHUB_REPOSITORY')!=REPO or os.environ.get('GITHUB_RUN_ATTEMPT')!='1':
        raise GlobalStop('Fixed repository and first attempt required')
    approved=os.environ.get('FROGMAN_APPROVED_PLAN_SHA256')
    if not approved:raise GlobalStop('No approval supplied')
    plan=json.loads(Path(os.environ['FROGMAN_PLAN']).read_bytes())
    if plan.get('plan_sha256')!=approved:raise GlobalStop('Approved plan differs')
    return run(plan,GitHubGit(REPO,os.environ['GITHUB_TOKEN']),MetaculusClient(os.environ['METACULUS_TOKEN']),
        Path(os.environ['RUNNER_TEMP'])/'acceptance-state',approved_plan_sha256=approved,
        run_id=os.environ['GITHUB_RUN_ID'],attempt=1,code_sha256=os.environ['FROGMAN_BUNDLE_SHA'],
        clock=lambda:dt.datetime.now(dt.timezone.utc))

if __name__=='__main__':main()
