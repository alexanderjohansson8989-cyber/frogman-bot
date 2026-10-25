import unittest
import tempfile
from network_archive_probe_v1 import run_probe
from test_git_state_store_v1 import FakeGit,NOW
from season_storage_simulation_v1 import simulate

class SeasonStorageTests(unittest.TestCase):
    def test_network_probe_contract_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=run_probe(FakeGit(),tmp,run_id='87654',code_sha256='b'*64,clock=lambda:NOW)
        self.assertTrue(r['exact_result_restored'])
        self.assertTrue(r['owner_released'])
        self.assertEqual(r['synthetic_model_transport_calls'],4)
        self.assertEqual(r['real_model_calls'],0)
    def test_three_full_chains_archive_and_restore(self):
        r=simulate(3)
        self.assertEqual(r['model_transport_calls'],12)
        self.assertEqual(r['restored_archive_questions'],[1,2,3])
        self.assertEqual(r['real_network_calls'],0)
        self.assertTrue(r['active_limits_pass'])
        self.assertTrue(r['dedup_and_permits_preserved'])

if __name__=='__main__':unittest.main()
