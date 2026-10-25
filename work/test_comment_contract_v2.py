"""Regressions against upstream ForecastSerializer, not latest-forecast fixtures."""
import copy,datetime as dt,unittest,urllib.parse
from candidate_api_v1 import MetaculusClient
from candidate_comments_v1 import verify,attached_values
from independent_runner import Stop
from test_candidate_runtime_v1 import FakeHTTP,serialized_comment_forecast
from test_acceptance_runtime_v1 import comment
from test_test_area_acceptance_v1 import plan
from test_competition_adapter_v1 import NOW,make_plan,post_fixture
from probe_comments_readonly_v1 import OneRead,PRIVATE_PATH

def cp(p):return dict(forecast_plan=p,payload=p['comment'],prepared_utc=NOW.isoformat())

class CommentContractTests(unittest.TestCase):
    def test_probe_transport_rejects_writes_other_paths_and_second_read(self):
        class HTTP:
            def call(self,*a,**kw):return {'results':[]}
        r=OneRead(HTTP(),PRIVATE_PATH)
        for path,kwargs in [('comments/create/',{}),(PRIVATE_PATH,{'method':'POST'}),(PRIVATE_PATH,{'payload':{}})]:
            with self.assertRaises(Stop):r.call(path,**kwargs)
        self.assertEqual(r.count,0)
        r.call(PRIVATE_PATH)
        with self.assertRaises(Stop):r.call(PRIVATE_PATH)

    def test_exact_query_uses_real_sort_field_and_bounded_private_filters(self):
        net=FakeHTTP([{'results':[],'next':None}]);c=MetaculusClient('synthetic',opener=net)
        c.comments(43327,310015,25)
        url,method,body=net.calls[0]
        self.assertEqual(method,'GET');self.assertIsNone(body)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query),
            dict(post=['43327'],author=['310015'],is_private=['true'],sort=['-created_at'],limit=['25'],offset=['25']))

    def test_forecast_serializer_without_myforecast_fields_passes(self):
        p=plan();c=comment(p)
        self.assertEqual(set(c['included_forecast']),{'start_time','probability_yes',
            'probability_yes_per_category','continuous_cdf','quartiles','scaling','options','question_type','question_unit'})
        self.assertTrue(verify(cp(p),c)['verified'])
        c['included_forecast']['start_time']=NOW.isoformat().replace('+00:00','Z')
        self.assertTrue(verify(cp(p),c)['verified'])

    def test_wrong_values_types_and_old_serializer_are_rejected(self):
        p=plan()
        for field,value in [('probability_yes',.6),('probability_yes',True),('probability_yes',float('nan')),
                ('question_type','numeric'),('start_time',NOW.timestamp()),('start_time','2026-10-15T18:00:00')]:
            c=comment(p);c['included_forecast'][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(Stop):verify(cp(p),c)

    def test_mc_and_numeric_serialized_values_keep_order_and_reject_mismatch(self):
        for kind in ('multiple_choice','numeric'):
            p=make_plan(post_fixture(kind));f=serialized_comment_forecast(p)
            self.assertEqual(attached_values(p,f),p['expected_forecast_values'])
            key='probability_yes_per_category' if kind=='multiple_choice' else 'continuous_cdf'
            f[key][-1]-=.1
            with self.subTest(kind=kind),self.assertRaises(Stop):attached_values(p,f)

    def test_mc_options_cannot_relabel_values(self):
        p=make_plan(post_fixture('multiple_choice'));f=serialized_comment_forecast(p)
        f['options']=list(reversed(f['options']))
        with self.assertRaises(Stop):attached_values(p,f)

if __name__=='__main__':unittest.main()
