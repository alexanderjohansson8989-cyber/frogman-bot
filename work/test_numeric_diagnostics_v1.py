import copy
import json
import math
import unittest
from statistics import NormalDist
from unittest.mock import patch
from numeric_diagnostics_v1 import raw_quantiles, projection_diagnostics
from candidate_profile_v1 import distribution_summary, review_payload
from competition_adapter_v1 import numeric_adaptation
from cdf_projection_v1 import project_cdf, raw_mixture_cdf


def mixture(mean=0., sd=1., low=None, high=None):
    return dict(kind='normal_mixture',components=[dict(weight=1.,mean=mean,sd=sd)],lower_bound=low,upper_bound=high)


def fields(grid, low=True, high=True):
    return dict(inbound_outcome_count=len(grid)-1,open_lower_bound=low,open_upper_bound=high,
                scaling=dict(continuous_range=grid))


class NumericDiagnosticsTests(unittest.TestCase):
    def test_unbounded_normal_matches_independent_standard_library(self):
        d=mixture(15.,3.);before=copy.deepcopy(d)
        r=raw_quantiles(d)
        self.assertEqual(r['status'],'available')
        for name,p in [('p10',.1),('p50',.5),('p90',.9)]:
            self.assertAlmostEqual(r['quantiles'][name],NormalDist(15,3).inv_cdf(p),places=6)
        self.assertEqual(d,before)

    def test_one_and_two_sided_truncation_against_analytic_probability_transform(self):
        normal=NormalDist()
        for lo,hi in [(0.,None),(None,0.),(-.5,1.5)]:
            with self.subTest(lo=lo,hi=hi):
                r=raw_quantiles(mixture(low=lo,high=hi))
                self.assertEqual(r['status'],'available')
                a=0 if lo is None else normal.cdf(lo);b=1 if hi is None else normal.cdf(hi)
                for name,p in [('p10',.1),('p50',.5),('p90',.9)]:
                    self.assertAlmostEqual(r['quantiles'][name],normal.inv_cdf(a+p*(b-a)),places=6)

    def test_each_truncated_component_normalized_before_mixing(self):
        d=mixture(low=0.,high=2.)
        d['components']=[dict(weight=.25,mean=-1.,sd=.7),dict(weight=.75,mean=1.5,sd=.3)]
        r=raw_quantiles(d);self.assertEqual(r['status'],'available')
        for name,p in [('p10',.1),('p50',.5),('p90',.9)]:
            x=r['quantiles'][name]
            expected=0.
            for c in d['components']:
                n=NormalDist(c['mean'],c['sd'])
                expected+=c['weight']*(n.cdf(x)-n.cdf(0))/(n.cdf(2)-n.cdf(0))
            self.assertAlmostEqual(expected,p,places=7)

    def test_small_spread_and_separated_scenarios(self):
        for d in [mixture(sd=1e-12),dict(mixture(),components=[dict(weight=.5,mean=-100.,sd=1.),dict(weight=.5,mean=100.,sd=1.)])]:
            r=raw_quantiles(d);self.assertEqual(r['status'],'available')
            q=r['quantiles'];self.assertLess(q['p10'],q['p50']);self.assertLess(q['p50'],q['p90'])
            self.assertAlmostEqual(q['p50'],0.)

    def test_far_truncated_tail_uses_log_space_and_finite_quantiles(self):
        r=raw_quantiles(mixture(low=40.,high=41.))
        self.assertEqual(r['status'],'available')
        self.assertTrue(all(40<v<41 for v in r['quantiles'].values()))
        self.assertLess(r['maximum_cdf_residual'],1e-7)

    def test_unrepresentable_or_invalid_diagnostics_do_not_raise(self):
        for d in [mixture(mean=1e308,sd=1e308),mixture(sd=0),mixture(low=2.,high=1.),
                  mixture(mean=1e20,sd=1e-20),{},mixture(mean=float('nan'))]:
            with self.subTest(d=d):self.assertEqual(raw_quantiles(d)['status'],'unavailable')
        with patch('numeric_diagnostics_v1.raw_mixture_cdf',side_effect=OverflowError()):
            self.assertEqual(distribution_summary(dict(distribution=mixture()))['raw_numeric_quantiles']['status'],'unavailable')

    def test_review_contains_original_distribution_and_explicit_raw_representation(self):
        a=dict(question_id='q',decision='forecast',distribution=mixture(),facts=[],assumptions=[],gaps=[],rationale='Future estimate.')
        before=copy.deepcopy(a)
        p=review_payload(dict(id='q',type='numeric',question='Future amount',criteria='Measured amount'),a,[])
        b=json.loads(p['messages'][0]['content'])
        self.assertEqual(b['answer'],a)
        self.assertEqual(b['distribution_summary']['raw_numeric_quantiles']['representation'],
                         'raw_continuous_latent_before_api_projection')
        self.assertEqual(a,before)

    def test_spike_floor_counts_tails_and_distances(self):
        f=fields([i/200 for i in range(201)])
        r=project_cdf([0.]*101+[1.]*100,f);before=copy.deepcopy(r)
        x=projection_diagnostics(r,f)
        self.assertEqual(x['status'],'available');self.assertEqual(x['raw_below_floor_count'],199)
        # Capping the spike redistributes mass above the floor in every bin.
        self.assertEqual(x['final_at_floor_count'],0)
        self.assertEqual(x['final_at_floor_fraction'],x['final_at_floor_count']/200)
        self.assertAlmostEqual(x['final_tail_masses']['lower'],r['continuous_cdf'][0])
        self.assertEqual(x['pmf_l1'],r['pmf_l1']);self.assertEqual(r,before)

    def test_valid_cdf_with_floor_bins_is_measured_without_forcing_projection(self):
        pmf=[.00005]*50+[(.998-.00005*50)/150]*150
        cdf=[.001]
        for mass in pmf:cdf.append(cdf[-1]+mass)
        f=fields([i/200 for i in range(201)])
        r=project_cdf(cdf,f)
        self.assertEqual(r['strategy'],'already_valid_api_rounding_only')
        self.assertEqual(projection_diagnostics(r,f)['final_at_floor_count'],50)

    def test_adapter_keeps_exact_projection_for_linear_log_and_discrete_grids(self):
        for grid in [[-5+i/20 for i in range(201)], [10**(i/100) for i in range(201)],[-.5,.5,1.5,2.5]]:
            for low,high in [(True,True),(False,False)]:
                f=fields(grid,low,high);d=mixture(mean=1,sd=.25)
                original=project_cdf(raw_mixture_cdf(d,grid),f)
                actual=numeric_adaptation(d,f)
                self.assertEqual({k:v for k,v in actual.items() if k!='diagnostics'},original)
                self.assertEqual(actual['diagnostics']['lower_tail_open'],low)
                self.assertEqual(actual['diagnostics']['interior_bucket_count'],len(grid)-1)

    def test_binary_and_categorical_summary_unchanged(self):
        self.assertEqual(distribution_summary(dict(distribution=dict(kind='binary',probability=.4))),dict(kind='binary',probability_yes=.4))
        self.assertEqual(distribution_summary(dict(distribution=dict(kind='categorical',probabilities={'a':.4,'b':.6}))),
                         dict(kind='categorical',probabilities={'a':.4,'b':.6}))


if __name__=='__main__':unittest.main()
