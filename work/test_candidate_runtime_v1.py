import copy,datetime as dt,json,tempfile,unittest
from pathlib import Path
from decimal import Decimal
from candidate_api_v1 import ClaudeClient,MetaculusClient
from candidate_dispatch_v1 import DispatchPermit,model_transport,forecast_transport
from candidate_recovery_v1 import RecoveryHost,inspect
from candidate_comments_v1 import submit as comment_submit,verify as verify_comment,comment_plan
from durable_candidate_host_v1 import DurableHost
from operations_ledger_v1 import GlobalStop,ReservedRequester
from independent_runner import Stop
from competition_adapter_v1 import submit_once
from advisor_policy import OPUS
from candidate_profile_v1 import OUT
from test_git_state_store_v1 import FakeGit,store,bootstrap,OWNER,OTHER,REPO
from test_competition_adapter_v1 import NOW,ME,make_plan,post_fixture,with_forecast

def terminal(run_id):return dict(id=int(run_id),run_attempt=1,status='completed',repository=dict(full_name=REPO))
def new_host(git,folder):return DurableHost(store(git),OWNER,folder,clock=lambda:NOW,credit_expires_utc='2026-10-21T00:00:00Z',key_expires_utc='2026-11-06T00:00:00Z')
def need():return dict(model=OPUS,system='Synthetic',messages=[dict(role='user',content='Source')],max_tokens=OUT['need'],output_config={'effort':'high'})
def raw(n=100):return dict(model=OPUS,stop_reason='end_turn',usage=dict(input_tokens=n,output_tokens=100),content=[])

class FakeHTTP:
    def __init__(self,replies=()):self.replies=list(replies);self.calls=[]
    def open(self,req,timeout):
        self.calls.append((req.full_url,req.get_method(),json.loads(req.data) if req.data else None))
        value=self.replies.pop(0)
        if isinstance(value,BaseException):raise value
        class Reply:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps(value).encode()
        return Reply()

class RuntimeTests(unittest.TestCase):
    def setUp(self):self.git=FakeGit();bootstrap(self.git)
    def test_model_and_metaculus_writes_are_disabled_without_permit(self):
        net=FakeHTTP();c=ClaudeClient('synthetic-test-secret',opener=net);m=MetaculusClient('synthetic-test-secret',opener=net)
        for action in (lambda:c.generate(need()),lambda:m.forecast([dict(question=102,probability_yes=.55)]),lambda:m.create_comment(make_plan()['comment'])):
            with self.assertRaises(GlobalStop):action()
        self.assertEqual(net.calls,[])
    def test_count_and_public_identity_use_fixed_paths(self):
        net=FakeHTTP([dict(input_tokens=123),dict(id=500,username='Frogman-bot',email='private@example.org')])
        self.assertEqual(ClaudeClient('synthetic-test-secret',opener=net).count(need()),123)
        self.assertEqual(MetaculusClient('synthetic-test-secret',opener=net).me(),ME)
        self.assertEqual(net.calls[0][0],'https://api.anthropic.com/v1/messages/count_tokens')
        self.assertNotIn('max_tokens',net.calls[0][2]);self.assertNotIn('output_config',net.calls[0][2])
    def test_large_count_is_returned_for_context_reduction_without_generation(self):
        net=FakeHTTP([dict(input_tokens=45000)])
        self.assertEqual(ClaudeClient('synthetic-test-secret',opener=net).count(need()),45000)
        self.assertEqual([x[0] for x in net.calls],['https://api.anthropic.com/v1/messages/count_tokens'])
    def test_journal_permit_is_single_use_even_if_new_wrapper_is_constructed(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);p=need();h.ledger.reserve('q/need/0','need',p,'.120480');h.ledger.dispatch('q/need/0',p)
            first=DispatchPermit(h.ledger,'q/need/0',p,kind='model',before_effect=h.before_effect)
            first(p)
            with self.assertRaises(GlobalStop):first(p)
            with self.assertRaises(GlobalStop):DispatchPermit(h.ledger,'q/need/0',p,kind='model',before_effect=h.before_effect)(p)
            with self.assertRaises(GlobalStop):h.close()
    def test_real_model_client_is_only_called_after_remote_permit(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);net=FakeHTTP([raw()]);client=ClaudeClient('synthetic-test-secret',opener=net)
            req=ReservedRequester(h.ledger,'q',transport=model_transport(h,client),count=lambda p:100,guard=h.before_effect)
            try:
                req('need',need())
                self.assertEqual(len(net.calls),1)
                self.assertEqual(net.calls[0][0],'https://api.anthropic.com/v1/messages')
                self.assertEqual(h.ledger.db.execute('SELECT kind FROM effect_permits').fetchall(),[('model',)])
            finally:h.close()
    def test_timeout_never_causes_client_or_requester_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);net=FakeHTTP([TimeoutError('synthetic-test-secret')]);client=ClaudeClient('synthetic-test-secret',opener=net)
            req=ReservedRequester(h.ledger,'q',transport=model_transport(h,client),count=lambda p:100,guard=h.before_effect)
            with self.assertRaises(GlobalStop) as err:req('need',need())
            self.assertNotIn('synthetic-test-secret',str(err.exception))
            with self.assertRaises(GlobalStop):req('need',need())
            self.assertEqual(len(net.calls),1)
            with self.assertRaises(GlobalStop):h.close()
    def test_forecast_deadline_crossed_during_permit_prevents_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);plan=make_plan();net=FakeHTTP();client=MetaculusClient('synthetic-test-secret',opener=net)
            key='500/33121/102';h.ledger.prepare_submission(key,plan);h.ledger.begin_submission(key,plan)
            clock=[NOW];h.clock=lambda:clock[0];original=h.before_effect
            def slow_guard():
                original();clock[0]=dt.datetime.fromisoformat(plan['binding']['cutoff_utc'])
            h.before_effect=slow_guard
            send=forecast_transport(h,client,plan)
            with self.assertRaises(GlobalStop):send(plan['payload'])
            self.assertEqual(net.calls,[])
            with self.assertRaises(GlobalStop):h.close()
    def stranded(self,folder,*,saved=True,n=100):
        h=new_host(self.git,folder);h.ledger.start_session('q',{},'1')
        h.ledger.reserve('q/need/0','need',need(),'.120480');h.ledger.dispatch('q/need/0',need())
        if saved:h.ledger.save_raw('q/need/0',raw(n))
        h.close()  # Active session deliberately retains the owner.
    def recovery(self,folder,get_run=terminal):return RecoveryHost(store(self.git),OTHER,folder,get_run=get_run,clock=lambda:NOW)
    def test_read_only_inspection_retains_unknown_reservation_and_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.stranded(tmp,saved=False);head=self.git.head(store(self.git).branch)
            r=inspect(store(self.git));self.assertTrue(r['blocked']);self.assertFalse(r['operations'][0]['raw_saved'])
            self.assertEqual(self.git.head(store(self.git).branch),head);self.assertEqual(r['owner'],OWNER)
    def test_running_wrong_repo_or_other_attempt_cannot_be_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.stranded(Path(tmp)/'old')
            for field,value in [('status','in_progress'),('run_attempt',2),('id',999),('repository',dict(full_name='other/repo'))]:
                with self.subTest(field=field),self.assertRaises(GlobalStop):
                    self.recovery(Path(tmp)/field,get_run=lambda run:dict(terminal(run),**{field:value}))
            self.assertEqual(inspect(store(self.git))['owner'],OWNER)
    def test_saved_usage_can_settle_but_interrupted_question_is_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.stranded(Path(tmp)/'old');r=self.recovery(Path(tmp)/'recovery')
            try:
                self.assertEqual(r.settle_saved(),dict(settled=['q/need/0'],unknown=[]))
                r.abandon_finished_work()
                self.assertEqual(r.ledger.db.execute('SELECT key,status FROM sessions').fetchall(),[('q','abandoned_recovered')])
                with self.assertRaises(GlobalStop):r.ledger.start_session('q',{},'1')
            finally:r.close()
            self.assertIsNone(inspect(store(self.git))['owner']);self.assertFalse(inspect(store(self.git))['blocked'])
    def test_unknown_usage_stays_reserved_and_cannot_be_abandoned(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.stranded(Path(tmp)/'old',saved=False);r=self.recovery(Path(tmp)/'recovery')
            try:
                self.assertEqual(r.settle_saved(),dict(settled=[],unknown=['q/need/0']))
                with self.assertRaises(GlobalStop):r.abandon_finished_work()
                with self.assertRaises(GlobalStop):DispatchPermit(r.ledger,'q/need/0',need(),kind='model',before_effect=lambda:None)(need())
            finally:r.close()
            self.assertTrue(inspect(store(self.git))['blocked'])
    def test_only_prepared_unsent_reservations_can_be_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');h.ledger.start_session('q',{},'1')
            h.ledger.reserve('q/need/0','need',need(),'.120480');h.close()
            r=self.recovery(Path(tmp)/'recovery')
            try:
                r.abandon_finished_work()
                self.assertEqual(r.ledger.available_units(),3000000)
                self.assertEqual(r.ledger.db.execute('SELECT state FROM operations').fetchone()[0],'cancelled_unsent')
                with self.assertRaises(GlobalStop):r.ledger.reserve('q/need/0','need',need(),'.120480')
            finally:r.close()
    def test_uncertain_forecast_can_be_recovered_by_reads_only(self):
        plan=make_plan();calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old')
            with self.assertRaises(Stop):submit_once(plan,h.ledger,fetch=lambda pid:(ME,post_fixture()),post_forecast=calls.append,clock=lambda:NOW,enabled=True)
            with self.assertRaises(GlobalStop):h.close()
            r=self.recovery(Path(tmp)/'recovery')
            try:
                result=r.readback_forecasts(lambda pid:(ME,with_forecast(post_fixture(),plan)))
                self.assertEqual(len(result['verified']),1);self.assertEqual(result['unknown'],[])
                self.assertEqual(len(calls),1)
                r.abandon_finished_work()
            finally:r.close()
    def test_recovered_count_overrun_persists_stop_even_when_cost_fits(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.stranded(Path(tmp)/'old',n=121);r=self.recovery(Path(tmp)/'recovery')
            try:
                with self.assertRaises(GlobalStop):r.settle_saved()
                self.assertEqual(r.ledger.db.execute('SELECT stopped FROM policy').fetchone()[0],1)
                with self.assertRaises(GlobalStop):r.abandon_finished_work()
            finally:r.close()

class CommentClient:
    def __init__(self,plan):self.plan=plan;self.rows=[];self.calls=0;self.timeout=False
    def fetch(self,pid):return ME,with_forecast(post_fixture(),self.plan)
    def comments(self,*args):return dict(results=copy.deepcopy(self.rows),next=None)
    def create_comment(self,payload,permit):
        permit(payload);self.calls+=1
        self.rows.append(dict(id=1,on_post=payload['on_post'],author=ME,is_private=True,is_soft_deleted=False,parent_id=None,
            text=payload['text'],created_at=(NOW+dt.timedelta(seconds=15)).isoformat(),
            included_forecast=with_forecast(post_fixture(),self.plan)['question']['my_forecasts']['latest']))
        if self.timeout:raise TimeoutError()
        return self.rows[-1]

class CommentTests(unittest.TestCase):
    def setUp(self):self.git=FakeGit();bootstrap(self.git);self.plan=make_plan()
    def verified_forecast(self,h):
        key='500/33121/102';h.ledger.prepare_submission(key,self.plan);h.ledger.begin_submission(key,self.plan)
        h.ledger.confirm_submission(key,self.plan,{'verified':True})
    def test_comment_disabled_and_requires_identical_verified_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);client=CommentClient(self.plan)
            try:
                with self.assertRaises(GlobalStop):comment_submit(self.plan,h,client)
                with self.assertRaises(GlobalStop):comment_submit(self.plan,h,client,enabled=True)
                self.assertEqual(client.calls,0)
            finally:h.close()
    def test_accepted_comment_with_lost_reply_is_read_back_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);self.verified_forecast(h);c=CommentClient(self.plan);c.timeout=True
            try:
                r=comment_submit(self.plan,h,c,enabled=True);self.assertTrue(r['verified']);self.assertEqual(c.calls,1)
                with self.assertRaises(GlobalStop):comment_submit(self.plan,h,c,enabled=True)
                self.assertEqual(c.calls,1)
            finally:h.close()
    def test_wrong_author_text_privacy_or_forecast_cannot_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);self.verified_forecast(h);c=CommentClient(self.plan)
            try:
                comment_submit(self.plan,h,c,enabled=True);p=comment_plan(self.plan,h.ledger,now=NOW)
                for field,value in [('text','Changed text'),('author',dict(id=999)),('is_private',False),('included_forecast',dict(forecast_values=[.5,.5]))]:
                    bad=dict(c.rows[0],**{field:value})
                    with self.subTest(field=field),self.assertRaises(Stop):verify_comment(p,bad)
            finally:h.close()
    def test_unknown_comment_can_be_recovered_without_any_new_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');self.verified_forecast(h);c=CommentClient(self.plan)
            saved_create=c.create_comment
            def lost(payload,permit):permit(payload);c.calls+=1;raise TimeoutError()
            c.create_comment=lost
            with self.assertRaises(Stop):comment_submit(self.plan,h,c,enabled=True)
            with self.assertRaises(GlobalStop):h.close()
            # Simulate the original accepted comment becoming visible later.
            c.create_comment=saved_create
            c.rows=[dict(id=9,on_post=101,author=ME,is_private=True,is_soft_deleted=False,parent_id=None,
                text=self.plan['comment']['text'],created_at=(NOW+dt.timedelta(seconds=15)).isoformat(),
                included_forecast=with_forecast(post_fixture(),self.plan)['question']['my_forecasts']['latest'])]
            r=RecoveryHost(store(self.git),OTHER,Path(tmp)/'recovery',get_run=terminal,clock=lambda:NOW)
            try:
                result=r.readback_comments(c)
                self.assertEqual(len(result['verified']),1);self.assertEqual(c.calls,1)
                r.abandon_finished_work()
            finally:r.close()

if __name__=='__main__':unittest.main()
