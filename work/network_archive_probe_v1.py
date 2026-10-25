"""One synthetic question with REAL private Git I/O; no model/Metaculus I/O."""
import datetime as dt,json,os,secrets,time
from collections import defaultdict
from pathlib import Path
from bounded_evidence import extract,digest
from candidate_api_v1 import ClaudeClient
from candidate_dispatch_v1 import model_transport
from durable_candidate_host_v1 import DurableHost,database_bytes
from git_state_store_v1 import SnapshotStore,owner_id
from git_timing_v1 import TimedGit
from operations_ledger_v1 import Ledger,GlobalStop
from question_archive_v1 import load_archived_session
from season_storage_simulation_v1 import ModelHTTP,OBS
from test_competition_adapter_v1 import post_fixture,bind_post,ME,NOW

REPO='alexanderjohansson8989-cyber/metaculus-bot'

def run_probe(git,folder,*,run_id,code_sha256,clock):
    if not str(run_id).isdigit():raise GlobalStop('Synthetic proof run identity required')
    events=[];timed=TimedGit(git,events);root=Path(folder);root.mkdir(parents=True,exist_ok=True)
    branch='frogman-state-archive-proof-'+str(run_id);period='synthetic-proof-'+str(run_id)
    def store():return SnapshotStore(timed,repository=REPO,branch=branch,period=period,cap_units=3000000,code_sha256=code_sha256,clock=clock)
    def host(label):
        expires=(clock()+dt.timedelta(minutes=20)).isoformat()
        return DurableHost(store(),owner_id(str(run_id),1,secrets.token_hex(16)),root/label,
            clock=clock,credit_expires_utc=expires,key_expires_utc=expires,archive_finished=True)
    l=Ledger(root/'initial.sqlite3',cap_usd='3',period_key=period)
    try:store().bootstrap({'journal.sqlite3':database_bytes(l)})
    finally:l.close()
    h=host('writer');net=ModelHTTP();started=time.perf_counter();before=len(events)
    def capture(q,urls,searched,path):
        path.mkdir(parents=True,exist_ok=True);(path/'synthetic.source').write_bytes(OBS.encode())
        return dict(documents=[extract(urls[0],dict(type='text',data=OBS),clock().isoformat())],failures=[])
    # Fixture metadata is synthetic and bound to its own fixture clock. Extend
    # only this synthetic binding's deadline to the proof's execution window.
    b=bind_post(post_fixture(),ME,tournament_id=33121,expected_username='Frogman-bot',now=NOW)
    b['cutoff_utc']=(clock()+dt.timedelta(minutes=20)).isoformat()
    try:
        r=h.run(b,transport=model_transport(h,ClaudeClient('synthetic-not-a-key',opener=net)),count=lambda p:100,
            capture_initial=capture,capture_follow=lambda *args:dict(documents=[],failures=[]),candidates=lambda *args:[])
        if r['status']!='prepared_local_only':raise GlobalStop('Synthetic question did not prepare')
        available=h.ledger.available_units()
        question_events=events[before:];question_seconds=time.perf_counter()-started
    finally:h.close()
    second=host('reader')
    try:
        key='500/33121/102';restored=load_archived_session(second.store,second.ledger.db,key)
        if restored['result']!=r or second.ledger.available_units()!=available:raise GlobalStop('Archive restart mismatch')
        try:second.ledger.start_session(key,{},'1')
        except GlobalStop:pass
        else:raise GlobalStop('Duplicate session permitted')
        archive_commit=second.ledger.db.execute('SELECT commit_sha FROM question_archives').fetchone()[0]
    finally:second.close()
    final=store();manifest,files=final.load();methods=defaultdict(lambda:dict(calls=0,seconds=0.0))
    for event in question_events:
        v=methods[event['method']];v['calls']+=1;v['seconds']+=event['seconds']
    return dict(version='network-archive-proof-v1',synthetic_only=True,real_model_calls=0,metaculus_calls=0,
        synthetic_model_transport_calls=net.calls,question_git_calls=len(question_events),
        question_git_seconds=sum(e['seconds'] for e in question_events),question_wall_seconds=question_seconds,
        git_method_timings=dict(methods),total_git_calls=len(events),branch=branch,head=final.head,
        sequence=manifest['sequence'],archive_commit=archive_commit,manifest=manifest,
        owner_released=manifest['owner'] is None,exact_result_restored=True,budget_and_duplicate_preserved=True,
        code_sha256=code_sha256,active_snapshot_bytes=sum(map(len,files.values())))

def main():
    from github_git_transport_v1 import GitHubGit
    if os.environ.get('GITHUB_REPOSITORY')!=REPO or os.environ.get('GITHUB_RUN_ATTEMPT')!='1':raise GlobalStop('First-attempt fixed repository only')
    git=GitHubGit(REPO,os.environ['GITHUB_TOKEN']);info=git.repository()
    if info.get('private') is not True:raise GlobalStop('Private proof repository required')
    if int(git.rate_limit.get('x-ratelimit-remaining') or 0)<1000:raise GlobalStop('Insufficient Git quota for bounded proof')
    r=run_probe(git,Path(os.environ['RUNNER_TEMP'])/'archive-network-proof',run_id=os.environ['GITHUB_RUN_ID'],
        code_sha256=os.environ['FROGMAN_BUNDLE_SHA'],clock=lambda:dt.datetime.now(dt.timezone.utc))
    print('ARCHIVE_NETWORK_RESULT '+json.dumps(r,sort_keys=True))

if __name__=='__main__':main()
