import copy
import datetime as dt
import unittest
from bounded_evidence import digest
from independent_runner import Stop
from competition_adapter_v1 import bind_post, bind_test_post, submit_once
from test_competition_adapter_v1 import post_fixture, NOW
from test_area_acceptance_v1 import ACTOR, AREA, prepare, check_plan, preflight, verify_readback
from probe_test_area_acceptance_v1 import ReadOnlyClient, LISTING, run


def fixture():
    p = post_fixture()
    p['projects']['tournament'] = [{'id': AREA}]
    p['question']['spot_scoring_time'] = '2026-10-14T00:00:00Z'
    return p


def plan(p=None):
    return prepare(p or fixture(), ACTOR, listing_post_ids=[101], now=NOW)


class AcceptanceTests(unittest.TestCase):
    def test_project_type_grouping_and_default_require_exact_area_id(self):
        for groups in ({'question_series':[{'id':AREA,'type':'question_series'}]},
                       {'default_project':{'id':AREA,'type':'question_series'}}):
            p=fixture();p['projects']=groups
            check_plan(plan(p))
        for groups in ({'category':[{'id':AREA}]}, {'default_project':{'id':AREA,'type':'category'}},
                       {'question_series':[{'id':str(AREA)}]}, {'question_series':[{'id':33121}]},None):
            p=fixture();p['projects']=groups
            with self.assertRaises(Stop): plan(p)

    def test_explicit_test_contract_keeps_real_times_and_fixed_scope(self):
        p = plan()
        check_plan(p)
        self.assertEqual(p['payload'], [{'question':102,'source':'api','probability_yes':.5}])
        self.assertFalse(p['submission_enabled'])
        self.assertTrue(p['comment']['is_private'])
        self.assertIn('not a researched forecast', p['comment']['text'])
        self.assertEqual(p['binding']['question']['timeline']['spot_scoring_time'], '2026-10-14T00:00:00+00:00')
        self.assertTrue(preflight(p, fixture(), ACTOR, listing_post_ids=[101], now=NOW+dt.timedelta(minutes=2)))
        # Production cannot silently adopt this close-only contract.
        with self.assertRaises(Stop):
            bind_post(fixture(), ACTOR, tournament_id=AREA, expected_username='Frogman-bot', now=NOW)
        with self.assertRaises(Stop):
            submit_once(p, None, fetch=lambda _: self.fail(), post_forecast=lambda _: self.fail(), clock=lambda:NOW, enabled=True)

    def test_scope_and_authenticated_preconditions(self):
        for field in ('actor','area','listing','kind','closed','future','previous','masked','group'):
            with self.subTest(field=field):
                p=fixture(); me=copy.deepcopy(ACTOR); ids=[101]
                if field=='actor': me['id']+=1
                elif field=='area': p['projects']['tournament']=[{'id':33121}]
                elif field=='listing': ids=[]
                elif field=='kind': p=post_fixture('multiple_choice');p['projects']['tournament']=[{'id':AREA}]
                elif field=='closed': p['status']='closed'
                elif field=='future': p['question']['open_time']='2027-01-01T00:00:00Z'
                elif field=='previous': p['question']['my_forecasts']['latest']={}
                elif field=='masked': p['question']['resolution_criteria']=None
                elif field=='group': p['group_of_questions']={'id':99}
                with self.assertRaises(Stop): prepare(p,me,listing_post_ids=ids,now=NOW)

    def test_preflight_rejects_mutated_expired_or_diagnostic_plans(self):
        frozen=plan()
        for change in ('payload','comment','scope','contract','expired','clock','diagnostic'):
            with self.subTest(change=change):
                p=copy.deepcopy(frozen);q=fixture();now=NOW
                if change=='payload': p['payload'][0]['probability_yes']=.6
                elif change=='comment': p['comment']['is_private']=False
                elif change=='scope':
                    p['binding']['tournament_id']=33121
                    p['plan_sha256']=digest({k:v for k,v in p.items() if k!='plan_sha256'})
                elif change=='contract': q['question']['fine_print']='Changed.'
                elif change=='expired': now=NOW+dt.timedelta(days=1)
                elif change=='clock': now=NOW-dt.timedelta(seconds=1)
                elif change=='diagnostic': p=bind_test_post(q,ACTOR,listing_post_ids=[101],now=NOW)
                with self.assertRaises(Stop): preflight(p,q,ACTOR,listing_post_ids=[101],now=now)

    def test_readback_requires_matching_fresh_actor_values_and_contract(self):
        p=plan(); post=fixture(); started=NOW+dt.timedelta(minutes=1);now=started+dt.timedelta(seconds=4)
        latest=dict(author_id=ACTOR['id'],question_id=102,start_time=started.timestamp()+1,
                    end_time=None,forecast_values=[.5,.5])
        post['question']['my_forecasts']['latest']=latest
        self.assertTrue(verify_readback(p,post,ACTOR,dispatch_started_utc=started.isoformat(),now=now)['verified'])
        for k,v in [('author_id',1),('question_id',101),('start_time',NOW.timestamp()),
                    ('start_time',float('nan')),('end_time',started.timestamp()),('forecast_values',[True,.5]),
                    ('forecast_values',[.4,.6]),('forecast_values',[.5])]:
            bad=copy.deepcopy(post);bad['question']['my_forecasts']['latest'][k]=v
            with self.subTest(k=k,v=v), self.assertRaises(Stop):
                verify_readback(p,bad,ACTOR,dispatch_started_utc=started.isoformat(),now=now)
        with self.assertRaises(Stop):
            verify_readback(p,post,ACTOR,dispatch_started_utc=started.isoformat(),now=started+dt.timedelta(minutes=6))

    def test_readonly_transport_rejects_write_paths_and_sixth_get(self):
        class Http:
            def __init__(self): self.calls=[]
            def call(self,path,**kwargs): self.calls.append((path,kwargs));return {}
        c=ReadOnlyClient('synthetic-test-token');c.http=Http()
        for path in ('questions/forecast/','comments/create/','https://evil.example/','posts/1/?x=other'):
            with self.assertRaises(Stop): c.get(path)
        for _ in range(5): c.get('users/me/')
        with self.assertRaises(Stop): c.get('users/me/')
        self.assertEqual(len(c.http.calls),5)
        self.assertTrue(all(kw=={'method':'GET'} for _,kw in c.http.calls))

    def test_probe_returns_minimal_original_and_no_sensitive_me_fields(self):
        class Client:
            count=0
            def get(self,path):
                self.count+=1
                if path=='users/me/': return dict(ACTOR,email='must-not-export@example.org')
                if path==LISTING:return {'results':[fixture()],'next':'untrusted-next'}
                return fixture()
        r=run(Client(),clock=lambda:NOW)
        self.assertEqual(r['get_attempts'],3)
        self.assertEqual(r['forecast_posts'],0)
        self.assertNotIn('email',r['actor'])
        self.assertEqual(r['status'],'plan_prepared_not_submitted')
        self.assertTrue(preflight(r['plan'],r['selected_input'],ACTOR,listing_post_ids=[101],now=NOW))


if __name__=='__main__': unittest.main()
