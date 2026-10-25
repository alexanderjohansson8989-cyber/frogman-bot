"""Disabled-by-default one-cycle publication entry. No bootstrap or scheduler.

An explicit reviewed launch binds config, runtime code and workflow identity.
This is a machine contract, not evidence that a human granted permission.
Do not deploy a launcher before the corresponding user authorization.
"""
import datetime as dt,json,os,re
from pathlib import Path
from bounded_evidence import digest
from general_evaluation import instant
from operations_ledger_v1 import GlobalStop
from production_entry_v1 import _run_core

WORKFLOW='.github/workflows/publication_cycle_v1.yml'

def validate_launch(config,*,run_id,attempt,code_sha256,source_commit,workflow_path,clock):
    if (config.get('activation')!='publish_one_cycle' or config.get('submit') is not True
            or type(config.get('max_questions')) is not int or config['max_questions']!=1
            or config.get('actor_id')!=310015 or type(config['actor_id']) is not int
            or config.get('username')!='Frogman-bot' or config.get('tournament_id')!=33121
            or type(config['tournament_id']) is not int
            or config.get('resume_only_when_pending') is not True or config.get('pending_limit')!=1
            or type(config['pending_limit']) is not int):
        raise GlobalStop('Explicit single-question production identity required')
    launch=config.get('launch')
    if not isinstance(launch,dict) or set(launch)!={'version','config_sha256','code_sha256','source_commit','workflow_path','nonce','expires_utc','approval_reference'}:
        raise GlobalStop('Reviewed launch contract required')
    body={k:v for k,v in config.items() if k!='launch'}
    if (not isinstance(run_id,str) or not re.fullmatch(r'[1-9][0-9]*',run_id)
            or not isinstance(code_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',code_sha256)
            or not isinstance(source_commit,str) or not re.fullmatch(r'[0-9a-f]{40}',source_commit)
            or launch['version']!='publication-launch-v2' or launch['config_sha256']!=digest(body)
            or launch['code_sha256']!=code_sha256 or launch['source_commit']!=source_commit
            or workflow_path!=WORKFLOW or launch['workflow_path']!=WORKFLOW
            or not isinstance(launch['nonce'],str) or not re.fullmatch(r'[0-9a-f]{32}',launch['nonce'])
            or attempt!=1 or type(attempt) is not int
            or not isinstance(launch['approval_reference'],str) or not launch['approval_reference'].strip()):
        raise GlobalStop('Launch code/config/workflow differs')
    if not clock()<instant(launch['expires_utc']) or instant(launch['expires_utc'])!=instant(config['job_deadline_utc']):
        raise GlobalStop('Launch expired or differs from guarded job deadline')


def claim_launch(host,config):
    """Consume the pre-issued nonce durably before models or Metaculus writes.

    Compaction keeps this table. A lost checkpoint is not retried. Another run
    needs a new explicitly issued launch, even when no question was available.
    """
    ledger=host.ledger
    with ledger.tx():
        ledger.guard()
        ledger.db.execute('''CREATE TABLE IF NOT EXISTS publication_launches (
            nonce TEXT PRIMARY KEY, config_sha TEXT NOT NULL, owner_json TEXT NOT NULL,
            claimed_utc TEXT NOT NULL)''')
        nonce=config['launch']['nonce']
        if ledger.db.execute('SELECT 1 FROM publication_launches WHERE nonce=?',(nonce,)).fetchone():
            raise GlobalStop('Publication launch already consumed')
        ledger.db.execute('INSERT INTO publication_launches VALUES(?,?,?,?)',
            (nonce,digest(config),json.dumps(host.owner,sort_keys=True),host.clock().isoformat()))


def run(config,folder,*,git,mc,claude,run_id,attempt,code_sha256,clock,
        source_commit=None,workflow_path=None,enabled=False):
    if enabled is not True:raise GlobalStop('Publication entrypoint disabled')
    validate_launch(config,run_id=run_id,attempt=attempt,code_sha256=code_sha256,
        source_commit=source_commit,workflow_path=workflow_path,clock=clock)
    return _run_core(config,folder,git=git,mc=mc,claude=claude,run_id=run_id,
        attempt=attempt,code_sha256=code_sha256,clock=clock,before_intake=lambda host:claim_launch(host,config))


def main():
    # Activation must precede config, credentials and all network access.
    if os.environ.get('FROGMAN_PUBLICATION_ENABLED')!='true':
        raise GlobalStop('Publication launcher not activated')
    from production_entry_v1 import REPO
    if os.environ.get('GITHUB_REPOSITORY')!=REPO:raise GlobalStop('Repository differs')
    config=json.loads(Path(os.environ['FROGMAN_INPUT']).read_bytes())
    if digest(config)!=os.environ['FROGMAN_INPUT_SHA']:raise GlobalStop('Frozen configuration changed')
    clock=lambda:dt.datetime.now(dt.timezone.utc)
    if (os.environ.get('GITHUB_WORKFLOW_REF')!=REPO+'/'+WORKFLOW+'@refs/heads/main'
            or os.environ.get('GITHUB_WORKFLOW_SHA')!=os.environ.get('GITHUB_SHA')):
        raise GlobalStop('Workflow source differs')
    args=dict(run_id=os.environ['GITHUB_RUN_ID'],attempt=int(os.environ['GITHUB_RUN_ATTEMPT']),
        code_sha256=os.environ['FROGMAN_BUNDLE_SHA'],source_commit=os.environ['GITHUB_SHA'],
        workflow_path=WORKFLOW,clock=clock)
    validate_launch(config,**args)
    from candidate_api_v1 import ClaudeClient,MetaculusClient
    from github_git_transport_v1 import GitHubGit
    result=run(config,Path(os.environ['RUNNER_TEMP'])/'publication-cycle',
        git=GitHubGit(REPO,os.environ['GITHUB_TOKEN'],clock=clock),
        mc=MetaculusClient(os.environ['METACULUS_TOKEN']),
        claude=ClaudeClient(os.environ['ANTHROPIC_API_KEY']),**args,enabled=True)
    print('PUBLICATION_CYCLE_RESULT '+json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=='__main__':main()
