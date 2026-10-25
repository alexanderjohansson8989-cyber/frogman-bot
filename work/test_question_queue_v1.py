import copy,datetime as dt,tempfile,unittest
from pathlib import Path
from question_queue_v1 import run_queue,session_key,run_host_queue
from operations_ledger_v1 import Ledger,GlobalStop
from test_competition_adapter_v1 import post_fixture,ME,NOW

def post(qid,seconds):
    p=post_fixture();p['id']=qid+100;p['question']['id']=qid
    p['question']['spot_scoring_time']=(NOW+dt.timedelta(seconds=seconds)).isoformat()
    return p

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'journal.sqlite3'
        self.ledger=Ledger(self.path,cap_usd='10',period_key='queue-test')
        self.addCleanup(lambda:self.ledger.close())
        self.now=NOW;self.calls=[];self.refreshes=[]
    def execute(self,b):
        self.calls.append(b['question_id']);k=session_key(b)
        self.ledger.start_session(k,b,'1.952')
        r={'status':'validation_stop'};self.ledger.finish_session(k,r);return r
    def runq(self,posts,**overrides):
        byid={p['id']:p for p in posts}
        def refresh(pid):self.refreshes.append(pid);return copy.deepcopy(byid[pid])
        kwargs=dict(tournament_id=33121,expected_username='Frogman-bot',clock=lambda:self.now,
            job_deadline_utc=(NOW+dt.timedelta(hours=1)).isoformat(),minimum_runtime_seconds=300,
            max_questions=10,refresh_post=refresh,run_binding=self.execute)
        kwargs.update(overrides)
        return run_queue(posts,ME,self.ledger,**kwargs)
    def test_deadline_order_distinct_ids_duplicates_and_restart(self):
        ps=[post(2,1800),post(1,900),post(1,900)];original=copy.deepcopy(ps)
        r=self.runq(ps);self.assertEqual(self.calls,[1,2]);self.assertEqual(ps,original)
        self.assertEqual(r['skipped'][0]['reason'],'duplicate_intake')
        self.ledger.close();self.ledger=Ledger(self.path,cap_usd='10',period_key='queue-test')
        self.calls.clear();self.refreshes.clear();r=self.runq(ps)
        self.assertEqual(self.calls,[]);self.assertEqual(self.refreshes,[])
        self.assertEqual(len(r['skipped']),3)
    def test_contradictory_duplicates_fail_before_any_spend(self):
        a=post(1,900);b=copy.deepcopy(a);b['question']['fine_print']='Changed'
        with self.assertRaises(GlobalStop):self.runq([a,b])
        self.assertEqual(self.calls,[])
    def test_expired_short_and_existing_server_forecast_are_skipped(self):
        old=post(1,-1);short=post(2,200);existing=post(3,900)
        existing['question']['my_forecasts']['latest']={'forecast_values':[.4,.6]}
        r=self.runq([old,short,existing,post(4,1200)])
        self.assertEqual(self.calls,[4]);self.assertEqual(len(r['skipped']),3)
    def test_job_margin_and_attempt_limit(self):
        r=self.runq([post(1,900)],job_deadline_utc=(NOW+dt.timedelta(seconds=300)).isoformat())
        self.assertEqual(r['stop_reason'],'job_time_margin');self.assertEqual(self.refreshes,[])
        r=self.runq([post(1,900),post(2,1000)],max_questions=1)
        self.assertEqual(self.calls,[1]);self.assertEqual(r['stop_reason'],'attempt_limit')
    def test_whole_question_budget_is_checked_before_refresh(self):
        self.ledger.reserve('old','test',{},'8.1')
        r=self.runq([post(1,900)])
        self.assertEqual(r['stop_reason'],'complete_question_budget')
        self.assertEqual(self.refreshes,[]);self.assertEqual(self.calls,[])
    def test_refresh_time_and_changed_question_do_not_generate(self):
        def refresh(pid):
            self.now+=dt.timedelta(seconds=700);return post(1,900)
        r=self.runq([post(1,900)],refresh_post=refresh)
        self.assertEqual(r['skipped'][0]['reason'],'time_margin_after_refresh')
        self.now=NOW
        r=self.runq([post(1,900)],refresh_post=lambda pid:post(1,1200))
        self.assertEqual(r['skipped'][0]['reason'],'snapshot_changed');self.assertEqual(self.calls,[])
    def test_unknown_effect_and_active_session_stop_whole_queue(self):
        self.ledger.reserve('pending','test',{},'1');self.ledger.dispatch('pending',{})
        with self.assertRaises(GlobalStop):self.runq([post(1,900)])
        self.assertEqual(self.refreshes,[])
    def test_active_owned_session_also_stops(self):
        self.ledger.start_session('owned',{},'1')
        with self.assertRaises(GlobalStop):self.runq([post(1,900)])
        self.assertEqual(self.calls,[])
    def test_global_stop_is_never_swallowed_as_refresh_failure(self):
        def blocked(pid):raise GlobalStop('ownership lost')
        with self.assertRaises(GlobalStop):self.runq([post(1,900)],refresh_post=blocked)
        def timeout(pid):raise TimeoutError()
        r=self.runq([post(1,900)],refresh_post=timeout)
        self.assertEqual(r['skipped'][0]['reason'],'refresh_failed')
    def test_incomplete_executor_is_not_accepted(self):
        with self.assertRaises(GlobalStop):self.runq([post(1,900)],run_binding=lambda b:dict(status='candidate'))
    def test_global_stop_result_never_advances_to_next_question(self):
        r=self.runq([post(1,900),post(2,1000)],run_binding=lambda b:dict(status='global_stop'))
        self.assertEqual(len(r['attempted']),1);self.assertEqual(r['stop_reason'],'global_stop')
    def test_diagnostic_binding_cannot_replace_actual_post(self):
        from competition_adapter_v1 import bind_test_post
        p=post(1,-10)
        b=bind_test_post(p,ME,listing_post_ids=[p['id']],now=NOW)
        r=run_queue([b],ME,self.ledger,tournament_id=33121,expected_username='Frogman-bot',clock=lambda:NOW,
            job_deadline_utc=(NOW+dt.timedelta(hours=1)).isoformat(),minimum_runtime_seconds=300,max_questions=1,
            refresh_post=lambda pid:self.fail('Unexpected read'),run_binding=self.execute)
        self.assertEqual(self.calls,[]);self.assertEqual(r['skipped'][0]['reason'],'ineligible_snapshot')

    def test_durable_host_two_full_chains_and_restart_without_duplicate_effects(self):
        from test_git_state_store_v1 import FakeGit,bootstrap
        from test_candidate_runtime_v1 import new_host
        from season_storage_simulation_v1 import ModelHTTP,OBS
        from candidate_api_v1 import ClaudeClient
        from candidate_dispatch_v1 import model_transport
        from bounded_evidence import extract
        from question_archive_v1 import load_archived_session
        git=FakeGit();bootstrap(git);net=ModelHTTP();ps=[post(2,1800),post(1,900)]
        captured=[];byid={p['id']:p for p in ps}
        def capture(q,urls,searched,folder):
            captured.append(str(folder));folder.mkdir(parents=True,exist_ok=True)
            (folder/'source.txt').write_text(OBS,encoding='utf-8')
            return dict(documents=[extract(urls[0],dict(type='text',data=OBS),NOW.isoformat())],failures=[])
        for i in range(2):
            h=new_host(git,Path(self.tmp.name)/('host'+str(i)));h.archive_finished=True
            try:
                r=run_host_queue(h,ps,ME,tournament_id=33121,expected_username='Frogman-bot',
                    job_deadline_utc=(NOW+dt.timedelta(minutes=45)).isoformat(),minimum_runtime_seconds=300,
                    max_questions=2,refresh_post=lambda pid:copy.deepcopy(byid[pid]),
                    transport=model_transport(h,ClaudeClient('synthetic',opener=net)),count=lambda p:100,
                    capture_initial=capture,capture_follow=lambda *a:dict(documents=[],failures=[]),candidates=lambda *a:[])
                self.assertEqual([x['question_id'] for x in r['attempted']],[1,2] if i==0 else [])
                self.assertTrue(all(x['status']=='prepared_local_only' for x in r['attempted']))
                self.assertEqual(net.calls,8)
                self.assertEqual(h.ledger.db.execute('SELECT COUNT(*) FROM effect_permits').fetchone()[0],8)
                self.assertEqual(h.ledger.db.execute('SELECT COUNT(*) FROM submissions').fetchone()[0],0)
                self.assertEqual(h.ledger.db.execute('SELECT SUM(actual) FROM operations').fetchone()[0],19200)
                for qid in (1,2):
                    a=load_archived_session(h.store,h.ledger.db,f'500/33121/{qid}')
                    self.assertEqual(a['result']['status'],'prepared_local_only')
            finally:h.close()
        self.assertEqual(len(set(captured)),2)

if __name__=='__main__':unittest.main()
