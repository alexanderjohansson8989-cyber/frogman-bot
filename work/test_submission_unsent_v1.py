import datetime as dt,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from candidate_api_v1 import MetaculusClient
from candidate_dispatch_v1 import forecast_transport,record_submission_contract,DispatchPermit
from candidate_recovery_v1 import RecoveryHost,inspect
from candidate_comments_v1 import submit as submit_comment,comment_plan
from competition_adapter_v1 import submit_once
from operations_ledger_v1 import GlobalStop
from independent_runner import Stop
from test_candidate_runtime_v1 import new_host,terminal,FakeHTTP
from test_git_state_store_v1 import FakeGit,bootstrap,store,OTHER
from test_competition_adapter_v1 import make_plan,post_fixture,with_forecast,ME,NOW

KEY='500/33121/102'

class SubmissionUnsentTests(unittest.TestCase):
    def setUp(self):self.git=FakeGit();bootstrap(self.git)

    def close_interrupted(self,h):
        with self.assertRaises(GlobalStop):h.close()

    def recover(self,path):return RecoveryHost(store(self.git),OTHER,path,get_run=terminal,clock=lambda:NOW)

    def test_deadline_after_begin_forecast_releases_owner_without_http_or_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');p=make_plan();net=FakeHTTP();clock=[NOW]
            send=forecast_transport(h,MetaculusClient('synthetic',opener=net),p)
            original=h.ledger.begin_submission
            def delay(*a):original(*a);clock[0]=dt.datetime.fromisoformat(p['binding']['cutoff_utc'])
            with patch.object(h.ledger,'begin_submission',delay),self.assertRaises(Stop):
                submit_once(p,h.ledger,fetch=lambda _: (ME,post_fixture()),post_forecast=send,clock=lambda:clock[0],enabled=True)
            self.assertEqual(net.calls,[]);self.close_interrupted(h)
            r=self.recover(Path(tmp)/'recovery')
            self.assertEqual(r.cancel_proven_unsent_submissions(),dict(cancelled_unsent=[KEY],unknown=[]))
            self.assertEqual(r.cancel_proven_unsent_submissions(),dict(cancelled_unsent=[],unknown=[]))
            r.abandon_finished_work();r.close();self.assertIsNone(inspect(store(self.git))['owner'])
            h=new_host(self.git,Path(tmp)/'next')
            with self.assertRaises(GlobalStop):h.ledger.prepare_submission(KEY,p)
            h.close();self.assertEqual(net.calls,[])

    def test_comment_deadline_after_begin_preserves_verified_forecast(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');p=make_plan()
            h.ledger.prepare_submission(KEY,p);h.ledger.begin_submission(KEY,p);h.ledger.confirm_submission(KEY,p,{'verified':True})
            net=FakeHTTP([ME,with_forecast(post_fixture(),p)]);client=MetaculusClient('synthetic',opener=net)
            original=h.ledger.begin_submission
            def delay(*a):original(*a);h.clock=lambda:dt.datetime.fromisoformat(p['binding']['cutoff_utc'])
            with patch.object(h.ledger,'begin_submission',delay),self.assertRaises(GlobalStop):submit_comment(p,h,client,enabled=True)
            self.assertEqual([x[1] for x in net.calls],['GET','GET']);self.close_interrupted(h)
            r=self.recover(Path(tmp)/'recovery')
            self.assertEqual(r.cancel_proven_unsent_submissions()['cancelled_unsent'],[KEY+'/comment'])
            self.assertEqual(r.ledger.db.execute('SELECT state FROM submissions WHERE key=?',(KEY,)).fetchone()[0],'verified')
            r.abandon_finished_work();r.close();self.assertIsNone(inspect(store(self.git))['owner'])

    def strand(self,path,mode='attested',kind='forecast'):
        h=new_host(self.git,path);p=make_plan();key=KEY
        if kind=='comment':
            h.ledger.prepare_submission(KEY,p);h.ledger.begin_submission(KEY,p);h.ledger.confirm_submission(KEY,p,{'verified':True})
            p=comment_plan(p,h.ledger,now=NOW);key=p['key']
        h.ledger.prepare_submission(key,p)
        if mode!='legacy':record_submission_contract(h.ledger,key,p,kind=kind)
        h.ledger.begin_submission(key,p)
        if mode in ('permit','wrong_permit'):
            DispatchPermit(h.ledger,key,p['payload'],kind=kind if mode=='permit' else ('comment' if kind=='forecast' else 'forecast'),before_effect=h.before_effect)(p['payload'])
        if mode in ('receipt','plan','payload','protocol','kind','stopped'):
            with h.ledger.tx():
                if mode=='receipt':h.ledger.db.execute("UPDATE submissions SET receipt='{}' WHERE key=?",(key,))
                elif mode=='plan':h.ledger.db.execute("UPDATE submissions SET plan_sha='bad' WHERE key=?",(key,))
                elif mode=='stopped':h.ledger.db.execute('UPDATE policy SET stopped=1')
                else:
                    column={'payload':'payload_sha256','protocol':'protocol','kind':'kind'}[mode]
                    h.ledger.db.execute(f"UPDATE submission_dispatch_contracts SET {column}='bad' WHERE key=?",(key,))
        self.close_interrupted(h);return p,key

    def test_legacy_permitted_receipt_and_mismatches_stay_unknown_for_both_kinds(self):
        for kind in ('forecast','comment'):
            for mode in ('legacy','permit','wrong_permit','receipt','plan','payload','protocol','kind'):
                with self.subTest(kind=kind,mode=mode),tempfile.TemporaryDirectory() as tmp:
                    self.git=FakeGit();bootstrap(self.git);p,key=self.strand(Path(tmp)/'old',mode,kind)
                    r=self.recover(Path(tmp)/'recovery')
                    self.assertEqual(r.cancel_proven_unsent_submissions(),dict(cancelled_unsent=[],unknown=[key]))
                    with self.assertRaises(GlobalStop):r.abandon_finished_work()
                    r.close();self.assertTrue(inspect(store(self.git))['blocked'])

    def test_no_backfill_after_begin_or_in_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');p=make_plan();h.ledger.prepare_submission(KEY,p);h.ledger.begin_submission(KEY,p)
            with self.assertRaises(GlobalStop):record_submission_contract(h.ledger,KEY,p,kind='forecast')
            self.close_interrupted(h);r=self.recover(Path(tmp)/'recovery')
            with self.assertRaises(GlobalStop):record_submission_contract(r.ledger,KEY,p,kind='forecast')
            self.assertEqual(r.cancel_proven_unsent_submissions()['unknown'],[KEY]);r.close()

    def test_modified_client_and_injected_transports_receive_no_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);p=make_plan();c=MetaculusClient('synthetic',opener=FakeHTTP())
            c.forecast=lambda *a,**k:None
            self.assertFalse(hasattr(forecast_transport(h,c,p),'checkpoint_dispatch_contract'));h.close()

    def test_fixed_client_timeout_after_post_is_never_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');p=make_plan();net=FakeHTTP([TimeoutError()])
            send=forecast_transport(h,MetaculusClient('synthetic',opener=net),p)
            with self.assertRaises(GlobalStop):submit_once(p,h.ledger,fetch=lambda _: (ME,post_fixture()),post_forecast=send,clock=lambda:NOW,enabled=True)
            self.assertEqual(len(net.calls),1);self.close_interrupted(h)
            r=self.recover(Path(tmp)/'recovery');self.assertEqual(r.cancel_proven_unsent_submissions()['unknown'],[KEY])
            self.assertEqual(r.readback_forecasts(lambda _: (ME,with_forecast(post_fixture(),p)))['unknown'],[])
            r.abandon_finished_work();r.close();self.assertEqual(len(net.calls),1)

    def test_failed_cancellation_checkpoint_retains_remote_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(Path(tmp)/'old');r=self.recover(Path(tmp)/'recovery');self.git.fail='before'
            with patch('git_state_store_v1.time.sleep'),self.assertRaises(GlobalStop):r.cancel_proven_unsent_submissions()
            self.git.fail=None
            self.assertTrue(r.store.poisoned);self.assertEqual(inspect(store(self.git))['submissions'][0]['state'],'uncertain')
            with self.assertRaises(GlobalStop):r.close()
            self.assertIsNotNone(inspect(store(self.git))['owner'])

    def test_contract_checkpoint_failure_never_reaches_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');p=make_plan();net=FakeHTTP()
            send=forecast_transport(h,MetaculusClient('synthetic',opener=net),p)
            original=h.ledger.prepare_submission
            def fail_after_prepare(*a):original(*a);self.git.fail='before'
            with patch.object(h.ledger,'prepare_submission',fail_after_prepare),patch('git_state_store_v1.time.sleep'),self.assertRaises(GlobalStop):
                submit_once(p,h.ledger,fetch=lambda _: (ME,post_fixture()),post_forecast=send,clock=lambda:NOW,enabled=True)
            self.git.fail=None
            self.assertEqual(net.calls,[]);self.assertTrue(h.store.poisoned)
            self.assertEqual(inspect(store(self.git))['submissions'][0]['state'],'prepared')
            self.close_interrupted(h)

    def test_persistent_stop_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(Path(tmp)/'old','stopped');r=self.recover(Path(tmp)/'recovery')
            self.assertEqual(r.cancel_proven_unsent_submissions()['cancelled_unsent'],[KEY])
            self.assertEqual(r.ledger.db.execute('SELECT stopped FROM policy').fetchone()[0],1)
            with self.assertRaises(GlobalStop):r.abandon_finished_work()
            r.close()

    def test_running_owner_cannot_be_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(Path(tmp)/'old')
            with self.assertRaises(GlobalStop):
                RecoveryHost(store(self.git),OTHER,Path(tmp)/'recovery',get_run=lambda i:dict(terminal(i),status='in_progress'),clock=lambda:NOW)

if __name__=='__main__':unittest.main()
