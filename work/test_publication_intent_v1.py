import copy,datetime as dt,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import test_production_cycle_v1 as f
from test_git_state_store_v1 import store,OWNER
from test_candidate_runtime_v1 import terminal,new_host
from test_competition_adapter_v1 import NOW,ME,make_plan,post_fixture,with_forecast
from candidate_recovery_v1 import inspect
from durable_candidate_host_v1 import CheckpointLedger,DurableHost
from operations_ledger_v1 import GlobalStop,Ledger
from independent_runner import Stop
from publication_intent_v1 import advance,load,pending
from competition_adapter_v1 import verify_readback
from question_archive_v1 import compact_completed,load_archived_session
from recovery_intake_v1 import ReadOnlyRecoveryClient
from github_git_transport_v1 import GitHubGit

KEY='500/33121/102'
class Death(BaseException):pass


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.t=f.CycleTests();self.t.setUp();self.t.config['submit']=True
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.i=0
    def run_cycle(self,**kwargs):
        self.i+=1
        with patch.object(f,'OTHER',dict(OWNER,run_id=str(100+self.i))):
            return self.t.run_cycle(self.root/str(self.i),**kwargs)
    def db(self):
        _,files=store(self.t.git).load();db=sqlite3.connect(':memory:');db.deserialize(files['journal.sqlite3'])
        self.addCleanup(db.close);return db
    def posts(self,suffix):return sum(m=='POST' and u.endswith(suffix) for m,u in self.t.server.calls)
    def assert_complete(self):
        db=self.db()
        self.assertEqual(db.execute('SELECT state,forecast_verified,comment_verified FROM publications').fetchall(),[('complete',1,1)])
        self.assertEqual(self.posts('/questions/forecast/'),1);self.assertEqual(self.posts('/comments/create/'),1)
        self.assertEqual(self.t.model.model.calls,4)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM operations WHERE raw IS NOT NULL').fetchone(),(0,))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM question_archives').fetchone(),(1,))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM effect_permits').fetchone(),(6,))
    def strand_after_finish(self):
        original=CheckpointLedger.finish_session
        def crash(ledger,*a):original(ledger,*a);raise Death()
        def teardown(host):host.ledger.close();host.closed=True
        with patch.object(CheckpointLedger,'finish_session',crash),patch.object(DurableHost,'close',teardown),self.assertRaises(Death):
            self.run_cycle()
    def strand_comment_gap(self):
        def quota():
            return dict(remaining=1 if self.posts('/questions/forecast/') else 1000,
                observed_utc=NOW.isoformat(),reset_utc=(NOW+dt.timedelta(hours=1)).isoformat())
        with self.assertRaises(GlobalStop):self.run_cycle(quota=quota)
        self.assertEqual(self.db().execute('SELECT state FROM publications').fetchall(),[('pending',)])
    def test_success_compacts_and_does_not_resend_on_next_cycle(self):
        self.run_cycle();self.assert_complete();self.run_cycle();self.assert_complete()
    def test_atomic_intent_survives_death_before_first_post(self):
        self.strand_after_finish();db=self.db()
        self.assertEqual(db.execute('SELECT status FROM sessions').fetchall(),[('complete',)])
        self.assertEqual(db.execute('SELECT state FROM publications').fetchall(),[('pending',)])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM submissions').fetchone(),(0,))
        result=self.run_cycle();self.assertEqual(result['recovery']['status'],'recovered');self.assert_complete()
    def test_quota_gap_continues_comment_before_intake_without_models(self):
        self.strand_comment_gap()
        def listing(tid):
            self.assertEqual(self.posts('/comments/create/'),1)
            return [post_fixture()]
        r=self.run_cycle(listing=listing);self.assertEqual(r['continued'][0]['status'],'submitted_verified');self.assert_complete()
    def test_recovered_forecast_continues_comment(self):
        original=self.t.server.open;reads=[0]
        def lost(req,timeout):
            if '/posts/101/' in req.full_url:
                reads[0]+=1
                if reads[0]==3:raise TimeoutError()
            return original(req,timeout)
        self.t.server.open=lost
        with self.assertRaises(GlobalStop):self.run_cycle()
        r=self.run_cycle();self.assertTrue(r['recovery']['ready']);self.assert_complete()
    def test_unknown_comment_readback_never_posts_twice(self):
        self.t.server.fail_comments=True
        with self.assertRaises(GlobalStop):self.run_cycle()
        self.assertEqual(self.posts('/comments/create/'),1)
        r=self.run_cycle();self.assertFalse(r['recovery']['ready'])
        self.assertEqual(self.posts('/comments/create/'),1)
        self.t.server.fail_comments=False
        self.run_cycle();self.assert_complete()
    def test_after_verified_comment_crash_only_finishes_bookkeeping(self):
        import publication_intent_v1 as p
        original=p.finish
        def crash(*a,**kw):raise Death()
        with patch.object(p,'finish',crash),self.assertRaises(Death):self.run_cycle()
        self.run_cycle();self.assert_complete()
    def test_cutoff_marks_forecast_only_without_comment(self):
        self.strand_comment_gap();later=NOW+dt.timedelta(days=1)
        self.t.config['console_observed_utc']=later.isoformat()
        self.t.config['job_deadline_utc']=(later+dt.timedelta(minutes=45)).isoformat()
        r=self.run_cycle(clock=lambda:later,quota=lambda:dict(remaining=1000,observed_utc=later.isoformat(),reset_utc=(later+dt.timedelta(hours=1)).isoformat()))
        self.assertEqual(r['continued'][0]['status'],'forecast_only')
        self.assertEqual(r['continued'][0]['reason'],'cutoff');self.assertEqual(self.posts('/comments/create/'),0)
        self.assertEqual(self.db().execute('SELECT COUNT(*) FROM question_archives').fetchone(),(1,))
    def test_pending_intent_cannot_become_dryrun_or_new_research(self):
        self.strand_comment_gap();self.t.config['submit']=False
        with self.assertRaisesRegex(GlobalStop,'submission mode'):self.run_cycle()
        h=new_host(self.t.git,self.root/'manual')
        try:
            with self.assertRaisesRegex(GlobalStop,'Pending publication'):h.ledger.start_session('new',{},'1')
            with self.assertRaisesRegex(GlobalStop,'Pending publication'):compact_completed(h)
        finally:h.close()
        self.assertEqual(self.t.model.model.calls,4)
    def test_failed_atomic_registration_rolls_back_complete(self):
        from bounded_evidence import digest
        plan=make_plan()
        l=Ledger(self.root/'atomic.sqlite3',cap_usd='3',period_key='test')
        try:
            l.start_session(KEY,plan['binding'],'1')
            bad=copy.deepcopy(plan);bad['comment']['text']='changed'
            with self.assertRaises(Stop):l.finish_session(KEY,dict(status='prepared_local_only',submission_plan=bad),True)
            self.assertEqual(l.db.execute('SELECT status FROM sessions').fetchall(),[('active',)])
            self.assertEqual(l.db.execute('SELECT COUNT(*) FROM publications').fetchone(),(0,))
        finally:l.close()
    def test_changed_contract_is_journalled_without_any_post(self):
        original=self.t.server.open;reads=[0]
        def changed(req,timeout):
            if '/posts/101/' in req.full_url:
                reads[0]+=1
                if reads[0]==2:self.t.server.post['question']['fine_print']='Changed.'
            return original(req,timeout)
        self.t.server.open=changed;r=self.run_cycle()
        self.assertEqual(r['queue']['attempted'][0]['status'],'not_submitted')
        self.assertEqual(self.db().execute('SELECT state,reason FROM publications').fetchall(),[('not_submitted','Submission contract changed')])
        self.assertEqual(self.posts('/questions/forecast/'),0)
    def test_proven_unsent_attempt_is_terminal_without_reusing_key(self):
        import competition_adapter_v1 as a
        original=CheckpointLedger.begin_submission
        def crash(ledger,*args):original(ledger,*args);raise Death()
        def teardown(host):host.ledger.close();host.closed=True
        with patch.object(CheckpointLedger,'begin_submission',crash),patch.object(DurableHost,'close',teardown),self.assertRaises(Death):self.run_cycle()
        r=self.run_cycle()
        self.assertEqual(r['continued'][0]['status'],'not_submitted')
        self.assertEqual(r['continued'][0]['reason'],'attempt_closed_without_resend')
        self.assertEqual(self.posts('/questions/forecast/'),0)
        self.assertEqual(self.db().execute('SELECT state FROM submissions').fetchall(),[('cancelled_unsent',)])
    def test_archive_restores_frozen_plan_and_publication_events(self):
        self.run_cycle();db=self.db()
        restored=load_archived_session(store(self.t.git),db,KEY)
        self.assertEqual(restored['result']['status'],'prepared_local_only')
        self.assertTrue(any(e['action']=='publication-finish' for e in restored['events']))
        self.assertEqual(restored['result']['submission_plan']['payload'][0]['probability_yes'],.55)

    def test_atomic_remote_checkpoint_lost_before_or_after_commit(self):
        for after in (False,True):
            with self.subTest(after_commit=after):
                self.t=f.CycleTests();self.t.setUp();self.t.config['submit']=True
                original=CheckpointLedger.checkpoint;fired=[False]
                def fail(ledger):
                    if not fired[0] and ledger.detail and ledger.detail[0]=='session-finish':
                        fired[0]=True
                        if after:original(ledger)
                        ledger.store.poisoned=True
                        raise GlobalStop('Synthetic uncertain Git checkpoint')
                    return original(ledger)
                with patch.object(CheckpointLedger,'checkpoint',fail),self.assertRaises(GlobalStop):self.run_cycle()
                db=self.db()
                self.assertEqual(db.execute('SELECT status FROM sessions').fetchone(),('complete' if after else 'active',))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM publications').fetchone(),(1 if after else 0,))
                self.run_cycle()
                if after:self.assert_complete()
                else:
                    self.assertEqual(self.posts('/questions/forecast/'),0)
                    self.assertEqual(self.db().execute('SELECT status FROM sessions').fetchone(),('abandoned_recovered',))
                    self.assertEqual(self.t.model.model.calls,4)

    def test_local_stop_allows_second_question_but_preserves_first_outcome(self):
        self.t.config['max_questions']=2
        posts={101:post_fixture(),201:post_fixture()}
        posts[201]['id']=201;posts[201]['question']['id']=202
        original=self.t.server.open;reads={101:0,201:0}
        def route(req,timeout):
            if '/posts/' in req.full_url:
                pid=int(req.full_url.split('/posts/')[1].split('/')[0]);reads[pid]+=1
                if pid==101 and reads[pid]==2:posts[pid]['question']['fine_print']='Changed after research.'
                self.t.server.post=posts[pid]
                self.t.server.calls.append((req.get_method(),req.full_url))
                return f.Reply(posts[pid])
            reply=original(req,timeout)
            if req.full_url.endswith('/questions/forecast/'):
                self.t.server.post['question']['my_forecasts']['latest']['question_id']=json.loads(req.data)[0]['question']
            if req.full_url.endswith('/comments/create/'):
                reply.value['on_post']=json.loads(req.data)['on_post']
            return reply
        self.t.server.open=route
        r=self.run_cycle(listing=lambda tid:copy.deepcopy(list(posts.values())))
        self.assertEqual([x['status'] for x in r['queue']['attempted']],['not_submitted','submitted_verified'])
        self.assertEqual(self.t.model.model.calls,8)
        self.assertEqual(self.posts('/questions/forecast/'),1)
        self.assertEqual(self.db().execute('SELECT COUNT(*) FROM question_archives').fetchone(),(2,))


class LifecycleTests(unittest.TestCase):
    def test_recovery_allows_only_bounded_actual_lifecycle_changes(self):
        plan=make_plan();post=with_forecast(post_fixture(),plan)
        later=NOW+dt.timedelta(days=2)
        post['status']=post['question']['status']='resolved'
        post['question']['actual_resolve_time']='2026-10-16T01:00:00Z'
        with self.assertRaises(Stop):verify_readback(plan,post,ME)
        self.assertTrue(verify_readback(plan,post,ME,lifecycle_at=later)['verified'])
        for field,value in [('fine_print','New criteria'),('scheduled_close_time','2026-10-18T00:00:00Z'),
            ('actual_close_time','2026-10-15T17:00:00Z'),('actual_resolve_time','2026-10-19T00:00:00Z')]:
            with self.subTest(field=field):
                bad=copy.deepcopy(post);bad['question'][field]=value
                with self.assertRaises(Stop):verify_readback(plan,bad,ME,lifecycle_at=later)
    def test_open_question_cannot_claim_recovery_lifecycle_exception(self):
        plan=make_plan();post=with_forecast(post_fixture(),plan)
        post['question']['actual_resolve_time']='2026-10-16T01:00:00Z'
        with self.assertRaises(Stop):verify_readback(plan,post,ME,lifecycle_at=NOW+dt.timedelta(days=2))


class RecoveryClockTests(unittest.TestCase):
    def test_read_budget_starts_at_first_get_and_total_budget_is_bounded(self):
        class Store:
            def assert_owner(self,owner):pass
        class Host:closed=False;store=Store();owner={}
        clock=[NOW];r=ReadOnlyRecoveryClient(None,Host(),lambda:clock[0])
        clock[0]+=dt.timedelta(seconds=120)
        self.assertEqual(r.call(lambda:1),1)
        clock[0]+=dt.timedelta(seconds=91)
        with self.assertRaises(GlobalStop):r.call(lambda:2)
        clock[0]=NOW;r=ReadOnlyRecoveryClient(None,Host(),lambda:clock[0])
        clock[0]+=dt.timedelta(seconds=301)
        with self.assertRaises(GlobalStop):r.call(lambda:3)


class QuotaTests(unittest.TestCase):
    def test_quota_observation_does_not_invent_freshness_or_call_endpoint(self):
        class Reply:
            headers={'x-ratelimit-limit':'1000','x-ratelimit-remaining':'700','x-ratelimit-reset':str(int(NOW.timestamp()+3600))}
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return b'{}'
        class Opener:
            calls=0
            def open(self,*a,**kw):self.calls+=1;return Reply()
        clock=[NOW];g=GitHubGit('owner/repo','synthetic',clock=lambda:clock[0]);g.opener=Opener()
        with self.assertRaises(GlobalStop):g.quota_observation()
        g.repository();first=g.quota_observation();clock[0]+=dt.timedelta(minutes=5)
        self.assertEqual(g.quota_observation(),first);self.assertEqual(g.opener.calls,1)
        self.assertEqual(first['remaining'],700)
        with self.assertRaises(GlobalStop):g._call('GET','/rate_limit')


if __name__=='__main__':unittest.main()
