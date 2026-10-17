"""Two-job synthetic Git storage proof; no Claude/Metaculus credentials or I/O."""
import datetime as dt
import json
import os
import secrets
import tempfile
from decimal import Decimal
from pathlib import Path
from git_state_store_v1 import SnapshotStore,owner_id,sha256
from durable_candidate_host_v1 import DurableHost,database_bytes,inspect_database
from github_git_transport_v1 import GitHubGit
from operations_ledger_v1 import Ledger,GlobalStop

REPOSITORY='alexanderjohansson8989-cyber/metaculus-bot'

def run_probe(phase,git,folder,*,run_id,attempt,code_sha256,clock,expected_head=None):
    if phase not in ('writer','reader'):raise GlobalStop('Unknown storage proof phase')
    actor=owner_id(run_id,attempt,secrets.token_hex(16))
    branch='frogman-state-probe-'+run_id
    period='synthetic-storage-proof-'+run_id
    s=SnapshotStore(git,repository=REPOSITORY,branch=branch,period=period,cap_units=3000000,code_sha256=code_sha256,clock=clock)
    root=Path(folder);root.mkdir(parents=True,exist_ok=True)
    if phase=='writer':
        initial=Ledger(root/'initial.sqlite3',cap_usd='3',period_key=period)
        try:s.bootstrap({'journal.sqlite3':database_bytes(initial)})
        finally:initial.close()
    else:
        s.load()
        if s.head!=expected_head or s.manifest['owner'] is not None:raise GlobalStop('Cross-job checkpoint differs')
    expiry=(clock()+dt.timedelta(hours=1)).isoformat()
    host=DurableHost(s,actor,root/'restored',clock=clock,credit_expires_utc=expiry,key_expires_utc=expiry)
    try:
        ledger=host.ledger
        if phase=='writer':
            ledger.start_session('synthetic-question',{'synthetic':True},'1')
            payload={'synthetic':True,'purpose':'storage proof; no model dispatch'}
            ledger.reserve('synthetic-call','storage-test',payload,'.01')
            ledger.dispatch('synthetic-call',payload)
            raw={'synthetic':True,'received':True,'no_external_call':True}
            ledger.save_raw('synthetic-call',raw)
            ledger.settle('synthetic-call','.001',raw)
            ledger.finish_session('synthetic-question',{'status':'storage-proof-complete'})
        else:
            if ledger.db.execute('SELECT key,state,actual FROM operations').fetchall()!=[('synthetic-call','settled',1000)]:
                raise GlobalStop('Synthetic operation did not survive job boundary')
            if ledger.db.execute('SELECT key,status FROM sessions').fetchall()!=[('synthetic-question','complete')]:
                raise GlobalStop('Synthetic session did not survive job boundary')
            archives=[json.loads(b) for n,b in s.files.items() if n.endswith('.json')]
            if not any(x['action']=='operation-request' and x['arguments'][2].get('synthetic') is True for x in archives):
                raise GlobalStop('Full synthetic request not restored')
    finally:host.close()
    m,files=s.load()
    result=dict(version='frogman-state-proof-v1',phase=phase,run_id=run_id,branch=branch,head=s.head,
        sequence=m['sequence'],owner_released=m['owner'] is None,
        database_sha256=sha256(files['journal.sqlite3']),files=len(files),
        synthetic_only=True,claude_calls=0,metaculus_calls=0,code_sha256=code_sha256)
    if inspect_database(files['journal.sqlite3'],s)['blocked']:raise GlobalStop('Proof ended with unresolved synthetic work')
    return result

def main():
    run_id=os.environ['GITHUB_RUN_ID'];attempt=int(os.environ['GITHUB_RUN_ATTEMPT'])
    if os.environ.get('GITHUB_REPOSITORY')!=REPOSITORY:raise GlobalStop('Unexpected proof repository')
    result=run_probe(os.environ['PROBE_PHASE'],GitHubGit(REPOSITORY,os.environ['GITHUB_TOKEN']),
        Path(os.environ['RUNNER_TEMP'])/'state-proof',run_id=run_id,attempt=attempt,
        code_sha256=os.environ['PROBE_BUNDLE_SHA'],clock=lambda:dt.datetime.now(dt.timezone.utc),
        expected_head=os.environ.get('EXPECTED_STATE_HEAD'))
    print('STATE_PROOF_RESULT '+json.dumps(result,sort_keys=True))
    with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as out:out.write('state_head='+result['head']+'\n')

if __name__=='__main__':main()
