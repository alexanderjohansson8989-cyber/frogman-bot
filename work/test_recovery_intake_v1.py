import datetime as dt,tempfile,unittest
from pathlib import Path
from candidate_dispatch_v1 import record_submission_contract,DispatchPermit
from candidate_recovery_v1 import inspect
from recovery_intake_v1 import recover_before_intake,ReadOnlyRecoveryClient
from operations_ledger_v1 import GlobalStop
from test_git_state_store_v1 import FakeGit,bootstrap,store,OTHER,OWNER
from test_candidate_runtime_v1 import new_host,terminal,CommentClient
from candidate_comments_v1 import comment_plan
from test_competition_adapter_v1 import NOW,ME,make_plan,post_fixture,with_forecast

KEY='500/33121/102'
class Reads:
    def __init__(self,post=None):self.post_value=post or post_fixture();self.calls=[]
    def me(self):self.calls.append('me');return ME
    def post(self,p):self.calls.append('post');return self.post_value
    def comments(self,*a):self.calls.append('comments');return dict(results=[],next=None)
    def forecast(self,*a,**k):raise AssertionError('No forecast write allowed')
    def create_comment(self,*a,**k):raise AssertionError('No comment write allowed')

class RecoveryIntakeTests(unittest.TestCase):
    def setUp(self):self.git=FakeGit();bootstrap(self.git)
    def recover(self,tmp,client=None,get_run=terminal):
        return recover_before_intake(store(self.git),OTHER,Path(tmp)/'recovery',client=client or Reads(),get_run=get_run,clock=lambda:NOW)
    def strand(self,tmp,*,permit=False,contract=True):
        h=new_host(self.git,Path(tmp)/'old');p=make_plan();h.ledger.prepare_submission(KEY,p)
        if contract:record_submission_contract(h.ledger,KEY,p,kind='forecast')
        h.ledger.begin_submission(KEY,p)
        if permit:DispatchPermit(h.ledger,KEY,p['payload'],kind='forecast',before_effect=h.before_effect)(p['payload'])
        with self.assertRaises(GlobalStop):h.close()
        return p
    def test_clean_state_never_claims_or_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            before=store(self.git);before.load();head=before.head;reads=Reads()
            self.assertEqual(self.recover(tmp,reads)['status'],'clean');self.assertEqual(reads.calls,[])
            after=store(self.git);after.load();self.assertEqual(after.head,head)
    def test_count_failure_before_first_operation_abandons_without_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');h.ledger.start_session('failed-count',{},'1');h.close()
            result=self.recover(tmp);self.assertTrue(result['ready']);self.assertEqual(result['gets'],0)
            state=inspect(store(self.git));self.assertIsNone(state['owner'])
            self.assertEqual(state['sessions'],[dict(key='failed-count',status='abandoned_recovered')])
            h=new_host(self.git,Path(tmp)/'next')
            with self.assertRaises(GlobalStop):h.ledger.start_session('failed-count',{},'1')
            h.close()
    def test_unsent_submission_cancels_without_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(tmp);result=self.recover(tmp)
            self.assertEqual((result['ready'],result['gets']),(True,0))
            self.assertEqual(inspect(store(self.git))['submissions'][0]['state'],'cancelled_unsent')
    def test_sent_submission_reads_back_without_resending(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.strand(tmp,permit=True);reads=Reads(with_forecast(post_fixture(),p));result=self.recover(tmp,reads)
            self.assertTrue(result['ready']);self.assertEqual(reads.calls,['me','post'])
            self.assertEqual(inspect(store(self.git))['submissions'][0]['state'],'verified')
    def test_unresolved_legacy_stays_owned_and_cannot_start_intake(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(tmp,contract=False);result=self.recover(tmp)
            self.assertFalse(result['ready']);self.assertEqual(result['status'],'operator_required')
            self.assertEqual(inspect(store(self.git))['owner'],OTHER)
            with self.assertRaises(GlobalStop):new_host(self.git,Path(tmp)/'next')
    def test_private_comment_is_read_back_without_new_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');p=make_plan()
            h.ledger.prepare_submission(KEY,p);h.ledger.begin_submission(KEY,p);h.ledger.confirm_submission(KEY,p,{'verified':True})
            cp=comment_plan(p,h.ledger,now=NOW);key=cp['key']
            h.ledger.prepare_submission(key,cp);record_submission_contract(h.ledger,key,cp,kind='comment');h.ledger.begin_submission(key,cp)
            c=CommentClient(p)
            c.create_comment(cp['payload'],DispatchPermit(h.ledger,key,cp['payload'],kind='comment',before_effect=h.before_effect))
            with self.assertRaises(GlobalStop):h.close()
            reads=Reads();reads.comments=c.comments
            result=self.recover(tmp,reads)
            self.assertTrue(result['ready']);self.assertEqual(result['gets'],1);self.assertEqual(c.calls,1)
            self.assertTrue(all(x['state']=='verified' for x in inspect(store(self.git))['submissions']))
    def test_active_or_wrong_workflow_never_takes_ownership(self):
        for mode in ('running','wrong'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                self.git=FakeGit();bootstrap(self.git);self.strand(tmp)
                if mode=='running':
                    result=self.recover(tmp,get_run=lambda i:dict(terminal(i),status='in_progress'))
                    self.assertEqual(result['status'],'busy')
                else:
                    with self.assertRaises(GlobalStop):self.recover(tmp,get_run=lambda i:dict(terminal(i),id=999))
                self.assertEqual(inspect(store(self.git))['owner'],OWNER)
    def test_read_failure_leaves_unknown_and_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(tmp,permit=True);reads=Reads()
            def fail():reads.calls.append('me');raise TimeoutError()
            reads.me=fail;result=self.recover(tmp,reads)
            self.assertFalse(result['ready']);self.assertEqual(reads.calls,['me'])
            self.assertEqual(result['gets'],1)
    def test_read_window_and_get_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);clock=[NOW];reader=ReadOnlyRecoveryClient(Reads(),h,lambda:clock[0],max_gets=1)
            with self.assertRaises(GlobalStop):reader.fetch(101)
            self.assertEqual(reader.gets,1)
            clock[0]=NOW+dt.timedelta(seconds=91)
            with self.assertRaises(GlobalStop):reader.comments(101,500)
            self.assertEqual(reader.gets,1);h.close()

if __name__=='__main__':unittest.main()
