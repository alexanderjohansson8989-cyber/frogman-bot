import copy,unittest
from mc_option_contract_v1 import option_contract,expected_values

class OptionPrototypeTests(unittest.TestCase):
    def test_legacy_nulls_preserve_raw_contract_and_option_order(self):
        q=dict(options=['Democrats','Republicans','Other'],all_options_ever=None,options_history=None);original=copy.deepcopy(q)
        c=option_contract(q)
        self.assertEqual(expected_values({'Other':.1,'Republicans':.5,'Democrats':.4},c),[.4,.5,.1])
        self.assertEqual(q,original);self.assertIsNone(c['raw_options_history'])
    def test_history_preserves_inactive_options_and_catchall(self):
        q=dict(options=['A','Other'],all_options_ever=['A','B','Other'],options_history=[['old',['A','B','Other']],['new',['A','Other']]])
        c=option_contract(q);self.assertEqual(expected_values({'A':.7,'Other':.3},c),[.7,None,.3])
    def test_conflicts_duplicates_and_missing_probabilities_stop(self):
        for q in (dict(options=['A','A']),dict(options=['A','B'],all_options_ever=['B','C']),
                  dict(options=['A','B'],all_options_ever=['B','A'],options_history=[['old',['A','B']]])):
            with self.assertRaises(ValueError):option_contract(q)
        with self.assertRaises(ValueError):expected_values({'A':1},option_contract(dict(options=['A','B'])))

if __name__=='__main__':unittest.main()
