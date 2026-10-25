import copy,math,random,unittest
from cdf_projection_v1 import project_cdf,masses
from numeric_cdf import validate_numeric_cdf

def fields(n=200,lower=True,upper=True):
    return dict(inbound_outcome_count=n,open_lower_bound=lower,open_upper_bound=upper,
                scaling={'continuous_range':[i/n for i in range(n+1)]})

class ProjectionTests(unittest.TestCase):
    def test_spike_satisfies_minimum_maximum_tails_and_exact_sum(self):
        original=[0.]*101+[1.]*100;saved=copy.deepcopy(original)
        r=project_cdf(original,fields());v=masses(r['continuous_cdf'])
        self.assertEqual(original,saved);self.assertEqual(validate_numeric_cdf(r['continuous_cdf'],fields()),r['continuous_cdf'])
        self.assertGreaterEqual(v[0],.001);self.assertGreaterEqual(v[-1],.001-1e-12)
        self.assertLessEqual(max(v[1:-1]),.2+1e-12);self.assertAlmostEqual(sum(v),1,places=13)
        self.assertGreaterEqual(r['pmf_l1'],1.6-1e-8)
        self.assertEqual(project_cdf(r['continuous_cdf'],fields())['continuous_cdf'],r['continuous_cdf'])
    def test_already_valid_cdf_is_preserved_at_api_precision(self):
        original=[.001+.998*i/200 for i in range(201)]
        r=project_cdf(original,fields());self.assertEqual(r['continuous_cdf'],validate_numeric_cdf(original,fields()))
    def test_all_open_closed_combinations_and_nonstandard_counts(self):
        for n in (1,2,17,199,200,201,2000):
            for low in (False,True):
                for high in (False,True):
                    with self.subTest(n=n,lower=low,upper=high):
                        f=fields(n,low,high);v=project_cdf([0.]*(n//2+1)+[1.]*(n-n//2),f)['continuous_cdf']
                        self.assertEqual(v,validate_numeric_cdf(v,f))
                        if not low:self.assertEqual(v[0],0)
                        if not high:self.assertEqual(v[-1],1)
    def test_random_cdfs_are_valid_and_original_is_unchanged(self):
        rng=random.Random(17)
        for _ in range(100):
            c=sorted(rng.random() for _ in range(201));original=c[:]
            r=project_cdf(c,fields());self.assertEqual(c,original)
            self.assertEqual(r['continuous_cdf'],validate_numeric_cdf(r['continuous_cdf'],fields()))
    def test_invalid_probabilities_and_inconsistent_grids_are_not_hidden(self):
        for c in ([0.]*200,[0.]*100+[float('nan')]+[1.]*100,[0.]*100+[-.1]+[1.]*100,[0.]*100+[.6,.5]+[1.]*99):
            with self.assertRaises(ValueError):project_cdf(c,fields())
        bad=fields();bad['scaling']['continuous_range'][1]=bad['scaling']['continuous_range'][0]
        with self.assertRaises(ValueError):project_cdf([0.]*101+[1.]*100,bad)

if __name__=='__main__':unittest.main()
