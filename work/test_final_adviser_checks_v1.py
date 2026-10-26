"""Durable audit of the same nonce transaction, with no extra checkpoint."""
import json,sqlite3,tempfile,unittest
from pathlib import Path
from bounded_evidence import digest
from durable_candidate_host_v1 import DurableHost
from publication_entry_v1 import claim_launch
from operations_ledger_v1 import GlobalStop
import test_git_state_store_v1 as f
import test_publication_entry_v1 as launch

class FinalAdviserTests(unittest.TestCase):
    def test_nonce_event_and_table_share_one_durable_checkpoint(self):
        git=f.FakeGit();s=f.bootstrap(git)
        with tempfile.TemporaryDirectory() as tmp:
            host=DurableHost(s,f.OWNER,Path(tmp)/'host',clock=lambda:launch.NOW,
                credit_expires_utc='2026-10-21T00:00:00Z',key_expires_utc='2026-11-06T00:00:00Z')
            try:
                p=launch.config();prior=s.manifest['sequence'];detail=host.ledger.detail
                claim_launch(host,p)
                self.assertEqual(s.manifest['sequence'],prior+1)
                self.assertEqual(host.ledger.detail,detail)
                _,files=f.store(git).load();db=sqlite3.connect(':memory:')
                try:
                    db.deserialize(files['journal.sqlite3'])
                    self.assertEqual(db.execute("SELECT count(*) FROM checkpoint_events WHERE action='publication-launch'").fetchone(),(1,))
                    self.assertEqual(db.execute('SELECT nonce,config_sha FROM publication_launches').fetchall(),[(p['launch']['nonce'],digest(p))])
                finally:db.close()
                with self.assertRaisesRegex(GlobalStop,'already consumed'):claim_launch(host,p)
                self.assertEqual(host.ledger.detail,detail)
            finally:host.close()

if __name__=='__main__':unittest.main()
