"""Adviser claims reproduced with the real cycle and synthetic transports."""
import unittest
from unittest.mock import patch
from operations_ledger_v1 import GlobalStop
from durable_candidate_host_v1 import CheckpointLedger,DurableHost
import test_publication_intent_v1 as f
from test_git_state_store_v1 import store


class AdviserTests(unittest.TestCase):
    def setUp(self):
        self.t=f.PublicationTests();self.t.setUp();self.addCleanup(self.t.doCleanups)

    def test_cancelled_comment_remains_explicit_forecast_only_without_resend(self):
        original=CheckpointLedger.begin_submission
        def crash(ledger,key,*args):
            original(ledger,key,*args)
            if key.endswith('/comment'):raise f.Death()
        def death_close(host):host.ledger.close();host.closed=True
        with patch.object(CheckpointLedger,'begin_submission',crash),patch.object(DurableHost,'close',death_close),self.assertRaises(f.Death):
            self.t.run_cycle()
        r=self.t.run_cycle()
        self.assertEqual(r['continued'][0]['status'],'forecast_only')
        self.assertEqual(r['continued'][0]['reason'],'attempt_closed_without_resend')
        self.assertEqual(self.t.posts('/questions/forecast/'),1)
        self.assertEqual(self.t.posts('/comments/create/'),0)
        self.assertEqual(self.t.db().execute('SELECT state FROM submissions WHERE key LIKE ?',('%/comment',)).fetchall(),[('cancelled_unsent',)])

    def test_readback_then_prepare_stops_but_publication_run_closes_without_resend(self):
        self.t.t.server.fail_comments=True
        with self.assertRaises(GlobalStop):self.t.run_cycle()
        self.t.t.server.fail_comments=False;self.t.t.config['submit']=False
        with self.assertRaisesRegex(GlobalStop,'submission mode'):self.t.run_cycle()
        db=self.t.db()
        self.assertEqual(db.execute('SELECT state FROM submissions').fetchall(),[('verified',),('verified',)])
        self.assertEqual(db.execute('SELECT state FROM publications').fetchall(),[('pending',)])
        self.assertEqual(self.t.posts('/comments/create/'),1)
        self.t.t.config['submit']=True;self.t.run_cycle();self.t.assert_complete()

    def test_new_code_cannot_silently_read_old_journal_or_reset_attempts(self):
        self.t.run_cycle();before=store(self.t.t.git);m,files=before.load();head=before.head
        changed=store(self.t.t.git);changed.code='f'*64
        with self.assertRaisesRegex(GlobalStop,'policy/schema/code'):changed.load()
        after=store(self.t.t.git);m2,files2=after.load()
        self.assertEqual((head,m,files),(after.head,m2,files2))
        self.t.run_cycle();self.t.assert_complete()


if __name__=='__main__':unittest.main()
