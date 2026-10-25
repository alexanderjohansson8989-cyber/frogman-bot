import copy,datetime as dt,json,tempfile,unittest
from pathlib import Path
from production_cycle_v1 import run_cycle
from candidate_api_v1 import ClaudeClient,MetaculusClient
from candidate_recovery_v1 import inspect
from bounded_evidence import extract
from operations_ledger_v1 import GlobalStop
from season_storage_simulation_v1 import ModelHTTP,OBS
from test_git_state_store_v1 import FakeGit,bootstrap,store,OWNER,OTHER
from test_candidate_runtime_v1 import terminal,new_host
from test_competition_adapter_v1 import NOW,ME,post_fixture
from advisor_policy import OPUS

class Reply:
    def __init__(self,value):self.value=value
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,n):return json.dumps(self.value).encode()

class Model:
    def __init__(self):self.model=ModelHTTP();self.counts=0
    def open(self,req,timeout):
        if req.full_url.endswith('/models/'+OPUS):return Reply(dict(id=OPUS))
        if req.full_url.endswith('/count_tokens'):self.counts+=1;return Reply(dict(input_tokens=100))
        return self.model.open(req,timeout)

class Server:
    def __init__(self):self.post=post_fixture();self.calls=[];self.comments=[];self.lose_forecast=False;self.fail_comments=False
    def open(self,req,timeout):
        url=req.full_url;method=req.get_method();p=json.loads(req.data) if req.data else None
        self.calls.append((method,url))
        if url.endswith('/users/me/'):return Reply(ME)
        if '/posts/101/' in url:return Reply(self.post)
        if url.endswith('/questions/forecast/'):
            v=p[0]['probability_yes'];self.post['question']['my_forecasts']['latest']=dict(author_id=500,question_id=102,
                start_time=(NOW+dt.timedelta(seconds=10)).timestamp(),end_time=None,forecast_values=[1-v,v])
            if self.lose_forecast:raise TimeoutError()
            return Reply({})
        if url.endswith('/comments/create/'):
            row=dict(id=99,on_post=101,author=ME,is_private=True,is_soft_deleted=False,parent_id=None,text=p['text'],
                created_at=(NOW+dt.timedelta(seconds=15)).isoformat(),included_forecast=dict(question_type='binary',
                probability_yes=self.post['question']['my_forecasts']['latest']['forecast_values'][1],start_time=(NOW+dt.timedelta(seconds=10)).isoformat()))
            self.comments.append(row);return Reply(row)
        if '/comments/?' in url:
            if self.fail_comments:raise TimeoutError()
            return Reply(dict(results=self.comments,next=None))
        raise AssertionError(url)

class CycleTests(unittest.TestCase):
    def setUp(self):
        self.git=FakeGit();bootstrap(self.git);s=store(self.git)
        self.config=dict(version='production-cycle-v1',submit=False,max_questions=1,actor_id=500,
            username='Frogman-bot',tournament_id=33121,cap_usd='3',period=s.period,console_balance_usd='3',
            console_observed_utc=NOW.isoformat(),credit_expires_utc='2026-10-21T00:00:00Z',
            key_expires_utc='2026-11-06T00:00:00Z',job_deadline_utc=(NOW+dt.timedelta(minutes=45)).isoformat(),
            minimum_runtime_seconds=300,git_reserve=600)
        self.model=Model();self.server=Server();self.events=[]
    def run_cycle(self,path,**kw):
        def capture(q,urls,searched,folder):
            return dict(documents=[extract(urls[0],dict(type='text',data=OBS),NOW.isoformat())],failures=[])
        def listing(tid):self.events.append('intake');return [copy.deepcopy(post_fixture())]
        args=dict(mc=MetaculusClient('synthetic',opener=self.server),claude=ClaudeClient('synthetic',opener=self.model),
            listing=listing,get_run=terminal,quota=lambda:dict(remaining=1000,observed_utc=NOW.isoformat(),reset_utc=(NOW+dt.timedelta(hours=1)).isoformat()),
            clock=lambda:NOW,capture_initial=capture,capture_follow=lambda *a:dict(documents=[],failures=[]),candidates=lambda *a:[],enabled=True)
        args.update(kw)
        return run_cycle(self.config,store(self.git),OTHER,path,**args)
    def test_disabled_cycle_has_no_reads_claim_or_spend(self):
        with tempfile.TemporaryDirectory() as tmp,self.assertRaisesRegex(GlobalStop,'disabled'):self.run_cycle(tmp,enabled=False)
        self.assertEqual(self.server.calls,[]);self.assertEqual(self.model.model.calls,0)
    def test_prepare_only_full_chain_and_second_cycle_never_regenerates(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=self.run_cycle(Path(tmp)/'first');self.assertTrue(r['owner_released'])
            self.assertEqual(r['queue']['attempted'][0]['status'],'prepared_local_only')
            self.assertEqual(self.model.model.calls,4);self.assertEqual(self.server.comments,[])
            r=self.run_cycle(Path(tmp)/'second');self.assertEqual(r['queue']['attempted'],[])
            self.assertEqual(self.model.model.calls,4)
    def test_submission_lost_reply_reads_forecast_and_one_private_comment(self):
        self.config['submit']=True;self.server.lose_forecast=True
        with tempfile.TemporaryDirectory() as tmp:
            r=self.run_cycle(tmp);self.assertEqual(r['queue']['attempted'][0]['status'],'submitted_verified')
            self.assertTrue(r['owner_released']);self.assertEqual(len(self.server.comments),1)
            self.assertEqual(len([m for m,u in self.server.calls if m=='POST']),2)
            self.assertTrue(all(x['state']=='verified' for x in inspect(store(self.git))['submissions']))
    def test_comment_read_failure_stops_and_preserves_owner(self):
        self.config['submit']=True;self.server.fail_comments=True
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(GlobalStop):self.run_cycle(tmp)
        s=inspect(store(self.git));self.assertTrue(s['blocked']);self.assertIsNotNone(s['owner'])
        self.assertEqual(self.model.model.calls,4);self.assertEqual(len(self.server.comments),1)
    def test_stale_budget_wrong_actor_and_low_quota_prevent_generation(self):
        for case in ('stale','actor','quota'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as tmp:
                self.setUp()
                if case=='stale':self.config['console_observed_utc']=(NOW-dt.timedelta(hours=2)).isoformat()
                if case=='actor':self.config['actor_id']=999
                kw={} if case!='quota' else dict(quota=lambda:dict(remaining=1))
                with self.assertRaises(GlobalStop):self.run_cycle(tmp,**kw)
                self.assertEqual(self.model.model.calls,0)
    def test_busy_previous_owner_blocks_intake_and_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');h.ledger.start_session('interrupted',{},'1');h.close()
            r=self.run_cycle(Path(tmp)/'new',get_run=lambda i:dict(terminal(i),status='in_progress'))
            self.assertFalse(r['recovery']['ready']);self.assertEqual(self.events,[]);self.assertEqual(self.model.model.calls,0)
    def test_finished_count_failure_recovers_before_new_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');h.ledger.start_session('old-failed-count',{},'1');h.close()
            r=self.run_cycle(Path(tmp)/'new');self.assertEqual(r['recovery']['status'],'recovered')
            self.assertEqual(self.events,['intake']);self.assertEqual(self.model.model.calls,4)

if __name__=='__main__':unittest.main()
