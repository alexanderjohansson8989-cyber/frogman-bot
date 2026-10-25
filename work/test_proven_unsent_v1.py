import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from candidate_api_v1 import ClaudeClient
from candidate_dispatch_v1 import model_transport,record_model_contract,DispatchPermit
from candidate_recovery_v1 import RecoveryHost,inspect
from operations_ledger_v1 import GlobalStop,ReservedRequester
from question_archive_v1 import compact_completed,load_archived_session
from test_candidate_runtime_v1 import new_host,need,raw,FakeHTTP,terminal
from test_git_state_store_v1 import FakeGit,bootstrap,store,OTHER,NOW
from test_question_archive_v1 import complete


class ProvenUnsentTests(unittest.TestCase):
    def setUp(self):self.git=FakeGit();bootstrap(self.git)

    def strand(self,folder,mode='attested'):
        h=new_host(self.git,folder);l=h.ledger;l.start_session('q',{},'1')
        with l.checkpoint_batch():
            l.reserve('q/need/0','need',need(),'.120480')
            if mode!='legacy':record_model_contract(l,'q/need/0',need())
            l.dispatch('q/need/0',need())
        if mode=='permit':DispatchPermit(l,'q/need/0',need(),kind='model',before_effect=h.before_effect)(need())
        if mode=='receipt':l.save_raw('q/need/0',raw())
        if mode=='wrong_contract':
            with l.tx():l.db.execute("UPDATE model_dispatch_contracts SET payload_sha256='bad'")
        if mode=='stopped':
            with l.tx():l.db.execute('UPDATE policy SET stopped=1')
        h.close()

    def recover(self,folder):return RecoveryHost(store(self.git),OTHER,folder,get_run=terminal,clock=lambda:NOW)

    def test_attested_no_permit_releases_reserve_but_never_retries_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(Path(tmp)/'old');r=self.recover(Path(tmp)/'recover')
            self.assertEqual(r.cancel_proven_unsent(),dict(cancelled_unsent=['q/need/0'],unknown=[]))
            self.assertEqual(r.cancel_proven_unsent(),dict(cancelled_unsent=[],unknown=[]))
            self.assertEqual(r.ledger.available_units(),3000000)
            self.assertEqual(r.ledger.db.execute('SELECT actual,raw,receipt_sha,state FROM operations').fetchone(),(None,None,None,'cancelled_unsent'))
            r.abandon_finished_work();r.close()
            h=new_host(self.git,Path(tmp)/'next')
            with self.assertRaises(GlobalStop):h.ledger.start_session('q',{},'1')
            with self.assertRaises(GlobalStop):h.ledger.reserve('q/need/0','need',need(),'.1')
            h.close()

    def test_missing_contract_permit_receipt_and_mismatch_remain_uncertain(self):
        for mode in ('legacy','permit','receipt','wrong_contract'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                self.git=FakeGit();bootstrap(self.git);self.strand(Path(tmp)/'old',mode)
                r=self.recover(Path(tmp)/'recover');balance=r.ledger.available_units()
                self.assertEqual(r.cancel_proven_unsent(),dict(cancelled_unsent=[],unknown=['q/need/0']))
                self.assertEqual(r.ledger.available_units(),balance)
                with self.assertRaises(GlobalStop):r.abandon_finished_work()
                r.close();self.assertTrue(inspect(store(self.git))['blocked'])

    def test_contract_cannot_be_backfilled_after_dispatch_or_during_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');h.ledger.start_session('q',{},'1')
            h.ledger.reserve('q/need/0','need',need(),'.1');h.ledger.dispatch('q/need/0',need())
            with self.assertRaises(GlobalStop):record_model_contract(h.ledger,'q/need/0',need())
            h.close();r=self.recover(Path(tmp)/'recover')
            with self.assertRaises(GlobalStop):record_model_contract(r.ledger,'q/need/0',need())
            r.close()

    def test_persistent_stop_is_not_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(Path(tmp)/'old','stopped');r=self.recover(Path(tmp)/'recover')
            r.cancel_proven_unsent()
            self.assertEqual(r.ledger.db.execute('SELECT stopped FROM policy').fetchone()[0],1)
            with self.assertRaises(GlobalStop):r.abandon_finished_work()
            r.close()

    def test_interruption_after_dispatch_before_transport_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');h.ledger.start_session('q',{},'1');net=FakeHTTP()
            def stop_after_checkpoint():
                if h.ledger.db.execute("SELECT 1 FROM operations WHERE state='uncertain'").fetchone():raise GlobalStop('Simulated interruption')
            req=ReservedRequester(h.ledger,'q',transport=h.model_transport(model_transport(h,ClaudeClient('synthetic',opener=net))),count=lambda p:100,guard=stop_after_checkpoint)
            with self.assertRaises(GlobalStop):req('need',need())
            self.assertEqual(net.calls,[]);h.close()
            r=self.recover(Path(tmp)/'recover');self.assertEqual(r.cancel_proven_unsent()['cancelled_unsent'],['q/need/0'])
            r.abandon_finished_work();r.close()

    def test_timeout_after_actual_dispatch_remains_unknown_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'old');h.ledger.start_session('q',{},'1');net=FakeHTTP([TimeoutError()])
            req=ReservedRequester(h.ledger,'q',transport=model_transport(h,ClaudeClient('synthetic',opener=net)),count=lambda p:100,guard=h.before_effect)
            with self.assertRaises(GlobalStop):req('need',need())
            h.close();r=self.recover(Path(tmp)/'recover')
            self.assertEqual(r.cancel_proven_unsent()['unknown'],['q/need/0']);self.assertEqual(len(net.calls),1)
            self.assertEqual(r.settle_saved()['unknown'],['q/need/0']);r.close()

    def test_failed_cancel_checkpoint_retains_remote_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(Path(tmp)/'old');r=self.recover(Path(tmp)/'recover');self.git.fail='before'
            with patch('git_state_store_v1.time.sleep'),self.assertRaises(GlobalStop):r.cancel_proven_unsent()
            self.git.fail=None
            self.assertEqual(inspect(store(self.git))['operations'][0]['state'],'uncertain')
            self.assertTrue(r.store.poisoned)
            with self.assertRaises(GlobalStop):r.abandon_finished_work()
            r.close()
            self.assertIsNotNone(inspect(store(self.git))['owner'])

    def test_recovered_and_next_complete_question_both_archive_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.strand(Path(tmp)/'old');r=self.recover(Path(tmp)/'recover')
            r.cancel_proven_unsent();r.abandon_finished_work();r.close()
            h=new_host(self.git,Path(tmp)/'next');expected=complete(h,'next')
            contracts=h.ledger.db.execute('SELECT * FROM model_dispatch_contracts ORDER BY key').fetchall()
            compact_completed(h)
            self.assertEqual(load_archived_session(h.store,h.ledger.db,'q')['result'],dict(status='abandoned_recovered'))
            self.assertEqual(load_archived_session(h.store,h.ledger.db,'next')['result'],expected)
            self.assertEqual(h.ledger.db.execute('SELECT * FROM model_dispatch_contracts ORDER BY key').fetchall(),contracts)
            h.close()


if __name__=='__main__':unittest.main()
