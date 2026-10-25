import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from checkpoint_objects_v1 import events_from_snapshot
from operations_ledger_v1 import GlobalStop
from test_candidate_runtime_v1 import new_host
from test_git_state_store_v1 import FakeGit, bootstrap, store, NOW
from season_storage_simulation_v1 import simulate


class CoalescingTests(unittest.TestCase):
    def setUp(self):
        self.git=FakeGit();bootstrap(self.git)

    def test_diagnostics_remain_exact_and_flush_before_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);before=len(self.git.updated)
            values=[{'step':i,'documents':[{'text':'source '*100}]} for i in range(3)]
            for v in values:h.save(v)
            self.assertEqual(len(self.git.updated),before)
            seen=[]
            def effect(payload):
                events=events_from_snapshot(store(self.git).load()[1])
                seen.extend(e['arguments'] for e in events if e['action']=='pipeline-result')
            h.model_transport(effect)({})
            self.assertEqual(seen,values)
            self.assertEqual(len(self.git.updated),before+1)
            h.close()

    def test_accounting_checkpoint_includes_pending_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);h.save({'step':1});before=len(self.git.updated)
            h.ledger.start_session('q',{},'1')
            self.assertEqual(len(self.git.updated),before+1)
            actions=[e['action'] for e in events_from_snapshot(store(self.git).load()[1])]
            self.assertEqual(actions[-2:],['pipeline-result','session-start'])
            self.assertFalse(h.ledger.pending);h.close()

    def test_stolen_branch_after_staging_never_dispatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);h.save({'step':1});original=self.git.head
            self.git.head=lambda branch:'f'*40
            calls=[]
            with self.assertRaises(GlobalStop):h.model_transport(calls.append)({})
            self.assertEqual(calls,[]);self.assertTrue(h.store.poisoned)
            self.git.head=original
            with self.assertRaises(GlobalStop):h.close()

    def test_deadline_during_flush_prevents_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);h.save({'step':1});clock=[NOW];h.clock=lambda:clock[0]
            update=self.git.update_ref
            def slow(*args,**kw):
                result=update(*args,**kw);clock[0]=h.expiry;return result
            self.git.update_ref=slow;calls=[]
            with self.assertRaises(GlobalStop):h.model_transport(calls.append)({})
            self.assertEqual(calls,[]);self.assertFalse(h.ledger.pending);h.close()

    def test_failed_flush_never_dispatches_or_retries_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);h.save({'step':1});self.git.fail='before';calls=[]
            with patch('git_state_store_v1.time.sleep'),self.assertRaises(GlobalStop):
                h.model_transport(calls.append)({})
            self.assertEqual(calls,[])
            self.git.fail=None
            with self.assertRaises(GlobalStop):h.before_effect()
            with self.assertRaises(GlobalStop):h.close()

    def test_clean_close_flushes_terminal_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);h.save({'status':'global_stop'});h.close()
            m,files=store(self.git).load()
            self.assertIsNone(m['owner'])
            self.assertEqual(events_from_snapshot(files)[-1]['arguments'],{'status':'global_stop'})

    def test_financial_actions_cannot_be_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp)
            for action in ('operation-raw','operation-dispatch','effect-permit','session-finish'):
                with self.assertRaises(GlobalStop):h.ledger.stage_diagnostic(action,{})
            h.close()

    def test_crash_loses_only_unflushed_diagnostic_and_blocks_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,Path(tmp)/'first');h.ledger.start_session('q',{},'1')
            head=h.store.head;h.save({'step':'local-only'})
            h.ledger.close()  # Simulate process loss: no host close/flush.
            self.assertEqual(self.git.head(h.store.branch),head)
            self.assertNotIn('pipeline-result',[e['action'] for e in events_from_snapshot(store(self.git).load()[1])])
            with self.assertRaises(GlobalStop):new_host(self.git,Path(tmp)/'second')

    def test_source_bytes_and_capture_event_remote_before_next_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);folder=Path(tmp)/'sources';folder.mkdir()
            def capture(*args):
                (folder/'page.source').write_bytes(b'synthetic evidence')
                return {'documents':[{'text':'synthetic evidence'}]}
            h.capture(capture)(folder)
            self.assertTrue(h.ledger.pending)
            def effect(payload):
                _,files=store(self.git).load()
                self.assertIn(b'synthetic evidence',files.values())
                self.assertEqual(events_from_snapshot(files)[-1]['action'],'source-capture')
            h.model_transport(effect)({});h.close()

    def test_three_questions_have_fewer_calls_and_restore(self):
        r=simulate(3)
        self.assertLess(r['question_git_calls_max'],300)
        self.assertEqual(r['restored_archive_questions'],[1,2,3])
        self.assertEqual(r['model_transport_calls'],12)
        self.assertTrue(r['dedup_and_permits_preserved'])


if __name__=='__main__':unittest.main()
