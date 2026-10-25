"""Synthetic full-pipeline season, fake HTTP/Git only. No credentials/network."""
import copy,hashlib,json,sqlite3,tempfile,time,zlib
from collections import Counter
from pathlib import Path
from advisor_policy import OPUS
from bounded_evidence import extract,digest
from candidate_api_v1 import ClaudeClient
from candidate_dispatch_v1 import model_transport
from durable_candidate_host_v1 import DurableHost,database_bytes
from git_state_store_v1 import SnapshotStore,MAX_FILE,MAX_FILES,MAX_TOTAL
from git_timing_v1 import TimedGit
from operations_ledger_v1 import Ledger,GlobalStop
from question_archive_v1 import load_archived_session
from test_git_state_store_v1 import FakeGit,REPO,OWNER,NOW
from test_competition_adapter_v1 import post_fixture,bind_post,ME

OBS='The synthetic historical baseline was 19 units.'

class CompressedBlobs(dict):
    def __setitem__(self,key,value):super().__setitem__(key,zlib.compress(value))
    def __getitem__(self,key):return zlib.decompress(super().__getitem__(key))

class ModelHTTP:
    def __init__(self):self.calls=0
    def open(self,req,timeout):
        self.calls+=1;p=json.loads(req.data);body=json.loads(p['messages'][0]['content'])
        if 'tools' in p:tag='research';a=dict(question_id=body['question']['id'],needs=['Find baseline'],source_urls=['https://example.org/source'])
        elif '<research_quality>' in p['system']:tag='research_quality';a=dict(findings=[],selected_urls=[])
        elif '<compact_review>' in p['system']:tag='compact_review';a=dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url='https://example.org/source',quote=OBS)])],unsupported=[],subjective=[])
        else:tag='forecast';a=dict(question_id=body['question']['id'],decision='forecast',distribution=dict(kind='binary',probability=.55),facts=[dict(claim=OBS,url='https://example.org/source',observation_date=None)],assumptions=['Future judgement.'],gaps=[],rationale='The synthetic baseline supports this subjective future assessment, with considerable uncertainty.')
        value=dict(model=OPUS,stop_reason='end_turn',usage=dict(input_tokens=100,output_tokens=100),content=[dict(type='text',text='<'+tag+'>'+json.dumps(a)+'</'+tag+'>')])
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps(value).encode()
        return Reply()


def simulate(n=400,archive=True):
    git=FakeGit();git.blobs=CompressedBlobs();timings=[];net=ModelHTTP()
    transport=TimedGit(git,timings)
    def store():return SnapshotStore(transport,repository=REPO,branch='frogman-state-season-synthetic',period='synthetic-season',cap_units=100000000,code_sha256='d'*64,clock=lambda:NOW)
    def capture(q,urls,searched,folder):
        folder.mkdir(parents=True,exist_ok=True)
        text=OBS+'\n'+q['id']+'\n'+''.join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(100))
        (folder/'synthetic.source').write_bytes(text.encode())
        return dict(documents=[extract(urls[0],dict(type='text',data=text),NOW.isoformat())],failures=[])
    rows=[];max_bytes=0;max_files=0;max_database=0;start=time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        initial=Ledger(Path(tmp)/'initial.sqlite3',cap_usd='100',period_key='synthetic-season')
        store().bootstrap({'journal.sqlite3':database_bytes(initial)});initial.close()
        def host(index):return DurableHost(store(),OWNER,Path(tmp)/('host-'+str(index)),clock=lambda:NOW,
            credit_expires_utc='2026-10-21T00:00:00Z',key_expires_utc='2026-11-01T00:00:00Z',archive_finished=archive)
        h=host(0)
        try:
            first_result=None
            for i in range(n):
                if i and i%25==0:
                    h.close();h=host(i)
                post=post_fixture();post['id']=100000+i;post['question']['id']=200000+i
                binding=bind_post(post,ME,tournament_id=33121,expected_username='Frogman-bot',now=NOW)
                before=len(timings);model_before=net.calls
                r=h.run(binding,transport=model_transport(h,ClaudeClient('synthetic',opener=net)),count=lambda p:100,
                    capture_initial=capture,capture_follow=lambda *a:dict(documents=[],failures=[]),candidates=lambda *a:[])
                assert r['status']=='prepared_local_only',r['status']
                if first_result is None:first_result=r
                calls=timings[before:];files=h.store.files
                db_size=len(files['journal.sqlite3']);total=sum(map(len,files.values()))
                max_bytes=max(max_bytes,total);max_files=max(max_files,len(files));max_database=max(max_database,db_size)
                rows.append(dict(question=i+1,git_calls=len(calls),model_calls=net.calls-model_before,
                    file_count=len(files),snapshot_bytes=total,database_bytes=db_size))
            expected_cost=n*4*2400
            assert h.ledger.available_units()==100000000-expected_cost
            assert h.ledger.db.execute('SELECT COUNT(*) FROM effect_permits').fetchone()[0]==n*4
            assert h.ledger.db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==n
            with _raises_stop():h.ledger.start_session('500/33121/200000',{},'1')
            restored=[]
            if archive:
                for i in sorted({0,n//2,n-1}):
                    key='500/33121/'+str(200000+i)
                    result=load_archived_session(h.store,h.ledger.db,key)['result']
                    assert digest(result)==h.ledger.db.execute('SELECT result_sha FROM sessions WHERE key=?',(key,)).fetchone()[0]
                    if i==0:assert result==first_result
                    restored.append(i+1)
            result=dict(version='synthetic-season-storage-v1',questions=n,archive_enabled=archive,
                model_transport_calls=net.calls,real_model_calls=0,real_network_calls=0,
                restarts=(n-1)//25,restored_archive_questions=restored,
                max_active_snapshot_bytes=max_bytes,max_active_files=max_files,max_active_database_bytes=max_database,
                active_limits_pass=max_bytes<=MAX_TOTAL and max_database<=MAX_FILE and max_files<=MAX_FILES,
                budget_unchanged_except_synthetic_usage=True,dedup_and_permits_preserved=True,
                total_fake_git_calls=len(timings),question_git_calls_min=min(x['git_calls'] for x in rows),
                question_git_calls_max=max(x['git_calls'] for x in rows),question_git_calls_mean=sum(x['git_calls'] for x in rows)/n,
                git_method_counts=dict(Counter(x['method'] for x in timings)),rows=rows,
                elapsed_local_seconds=time.perf_counter()-start,
                limitation='Synthetic binary questions with small sources; not real Git latency or production forecasts. Historical repository bytes still grow.')
        finally:h.close()
    return result


class _raises_stop:
    def __enter__(self):pass
    def __exit__(self,kind,value,tb):
        if kind is GlobalStop:return True
        if kind is None:raise AssertionError('Duplicate question was accepted')
        return False


if __name__=='__main__':
    import sys
    n=int(sys.argv[1]) if len(sys.argv)>1 else 400
    archive='--no-archive' not in sys.argv
    r=simulate(n,archive)
    folder=Path(__file__).resolve().parents[1]/'outputs/korningar/season_storage_20261018';folder.mkdir(parents=True,exist_ok=True)
    (folder/f'simulation_{n}_{"archive" if archive else "retained"}.json').write_text(json.dumps(r,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in r.items() if k!='rows'}))
