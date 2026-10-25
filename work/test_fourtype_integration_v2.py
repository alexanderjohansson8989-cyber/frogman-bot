import copy,datetime as dt,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from competition_adapter_v1 import bind_test_post,question_data,prepare,submit_once,bind_post
from candidate_service_v1 import run_bound_question
from candidate_profile_v1 import OUT,EFFORT,need_payload,payload
from operations_ledger_v1 import Ledger,GlobalStop,ReservedRequester
from test_competition_adapter_v1 import post_fixture,ME,NOW,candidate
from bounded_evidence import extract
from independent_runner import Stop
from advisor_policy import OPUS
from numeric_cdf import validate_numeric_cdf

OBS='The supplied baseline observation was 19 units.'

def synthetic_post(kind):
    p=post_fixture('numeric' if kind=='discrete' else kind);q=p['question']
    p['projects']['tournament']=[]
    if kind=='multiple_choice':q.update(all_options_ever=None,options_history=None)
    if kind=='discrete':
        q.update(type='discrete',inbound_outcome_count=71,open_lower_bound=True,open_upper_bound=True)
        q['scaling']=dict(range_min=.095,range_max=.805,nominal_min=.1,nominal_max=.8,
            continuous_range=[.095+.71*i/71 for i in range(72)],zero_point=None,
            inbound_outcome_count=71,open_lower_bound=True,open_upper_bound=True)
    if kind=='discrete':q['scaling']['continuous_range'][-1]=.805
    return p

def synthetic_transport(p):
    b=json.loads(p['messages'][0]['content'])
    if 'tools' in p:
        tag='research';a=dict(question_id=b['question']['id'],needs=['Find the baseline.'],source_urls=['https://example.org/source'])
    elif '<research_quality>' in p['system']:tag='research_quality';a=dict(findings=[],selected_urls=[])
    elif '<compact_review>' in p['system']:
        tag='compact_review';a=dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url='https://example.org/source',quote=OBS)])],unsupported=[],subjective=[])
    else:
        q=b['question'];tag='forecast'
        if q['type']=='binary':d=dict(kind='binary',probability=.55)
        elif q['type']=='categorical':d=dict(kind='categorical',probabilities={k:1/len(q['choices']) for k in q['choices']})
        else:
            edges=q['submission_constraints']['continuous_range'];width=edges[-1]-edges[0]
            d=dict(kind='normal_mixture',components=[dict(weight=1,mean=sum((edges[0],edges[-1]))/2,sd=width/20)],lower_bound=q['lower_bound'],upper_bound=q['upper_bound'])
        a=dict(question_id=q['id'],decision='forecast',distribution=d,facts=[dict(claim=OBS,url='https://example.org/source',observation_date=None)],
            assumptions=['Synthetic future judgement.'],gaps=[],rationale='The supplied baseline is used only for this synthetic transport test.')
    return dict(model=OPUS,stop_reason='end_turn',usage=dict(input_tokens=100,output_tokens=100),content=[dict(type='text',text='<'+tag+'>'+json.dumps(a)+'</'+tag+'>')])

def initial(q,urls,searched,folder):
    return dict(documents=[extract('https://example.org/source',dict(type='text',data=OBS),NOW.isoformat())],failures=[])

class FourTypeTests(unittest.TestCase):
    def test_run_question_through_prepare_for_all_four_types(self):
        for kind in ('binary','multiple_choice','numeric','discrete'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as folder:
                post=synthetic_post(kind);b=bind_test_post(post,ME,listing_post_ids=[post['id']],now=NOW)
                l=Ledger(Path(folder)/'journal.db',cap_usd='3',period_key='synthetic');calls=[]
                def transport(p):calls.append(copy.deepcopy(p));return synthetic_transport(p)
                try:
                    r=run_bound_question(b,folder,l,transport=transport,count=lambda p:100,clock=lambda:NOW,save=lambda r:None,
                        capture_initial=initial,capture_follow=lambda *a:dict(documents=[],failures=[]),candidates=lambda *a:[])
                    self.assertEqual(r['status'],'prepared_local_only',r.get('diagnostics'))
                    self.assertEqual(len(calls),4)
                    plan=r['submission_plan'];self.assertFalse(plan['submission_enabled'])
                    if kind in ('numeric','discrete'):
                        self.assertEqual(plan['payload'][0]['continuous_cdf'],validate_numeric_cdf(plan['expected_forecast_values'],post['question']))
                        self.assertIn('pmf_l1',plan['format_adaptation'])
                    if kind=='discrete':self.assertAlmostEqual(b['question']['discrete_outcomes']['centers'][0],.1)
                    touched=[]
                    with self.assertRaisesRegex(Stop,'Diagnostic'):
                        submit_once(plan,l,fetch=touched.append,post_forecast=touched.append,clock=lambda:NOW,enabled=True)
                    self.assertEqual(touched,[])
                finally:l.close()
    def test_test_area_exception_never_relaxes_production_spot_deadline(self):
        p=synthetic_post('numeric');p['projects']['tournament']=[{'id':33121}]
        p['question']['spot_scoring_time']='2026-10-14T00:00:00Z'
        with self.assertRaises(Stop):bind_post(p,ME,tournament_id=33121,expected_username='Frogman-bot',now=NOW)
        b=bind_test_post(p,ME,listing_post_ids=[p['id']],now=NOW)
        self.assertEqual(b['actual_spot_scoring_time'],p['question']['spot_scoring_time'])
        with self.assertRaises(Stop):bind_test_post(p,ME,listing_post_ids=[],now=NOW)
    def test_mc_null_history_and_conflicting_explicit_history(self):
        p=synthetic_post('multiple_choice');before=copy.deepcopy(p)
        self.assertEqual(question_data(p)['choices'],p['question']['options']);self.assertEqual(p,before)
        p['question']['all_options_ever']=['unrelated','other']
        with self.assertRaises(Stop):question_data(p)
    def test_dates_and_discrete_metadata_reach_model_without_relabeling(self):
        q=question_data(synthetic_post('discrete'))
        p=payload(q,initial(None,None,None,None)['documents'],NOW.isoformat());sent=json.loads(p['messages'][0]['content'])['question']
        self.assertEqual(sent['api_type'],'discrete')
        self.assertEqual(__import__('general_evaluation').instant(sent['timeline']['open_time']),
                         __import__('general_evaluation').instant('2026-10-14T00:00:00Z'))
        bad=synthetic_post('discrete');bad['question']['scaling']['nominal_min']=0
        with self.assertRaises(Stop):question_data(bad)
    def test_need_medium_and_forecast_6000_are_enforced_at_dispatch(self):
        self.assertEqual(OUT['forecast'],6000);self.assertEqual(EFFORT['need'],'medium')
        p=need_payload(question_data(synthetic_post('binary')),initial(None,None,None,None)['documents'],NOW.isoformat(),[],[])
        self.assertEqual(p['output_config'],{'effort':'medium'})
        with tempfile.TemporaryDirectory() as folder:
            l=Ledger(Path(folder)/'db',cap_usd='3',period_key='synthetic');calls=[]
            try:
                req=ReservedRequester(l,'q',transport=calls.append,count=lambda p:100,guard=lambda:None)
                p['output_config']={'effort':'high'}
                with self.assertRaises(GlobalStop):req('need',p)
                self.assertEqual(calls,[])
            finally:l.close()

if __name__=='__main__':unittest.main()
