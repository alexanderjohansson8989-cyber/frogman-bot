import copy,datetime as dt,json,tempfile,unittest
from pathlib import Path
from bounded_evidence import digest,extract
from candidate_profile_v1 import payload,audit_documents
from competition_adapter_v1 import bind_post,prepare,verify_readback,submit_once,reconcile
from operations_ledger_v1 import Ledger,GlobalStop
from independent_runner import Stop

NOW=dt.datetime(2026,10,15,18,tzinfo=dt.timezone.utc)
ME=dict(id=500,username='Frogman-bot')
def post_fixture(kind='binary'):
    q=dict(id=102,title='A future test question',type=kind,description='Background.',resolution_criteria='Use the specified future observation.',fine_print=None,
           status='open',resolution=None,open_time='2026-10-14T00:00:00Z',scheduled_close_time='2026-10-17T00:00:00Z',
           actual_close_time='2026-10-17T00:00:00Z',spot_scoring_time='2026-10-16T00:00:00Z',my_forecasts={'latest':None})
    if kind=='multiple_choice':q.update(options=['Active A','Active B'],all_options_ever=['Old','Active B','Active A'],options_history=[])
    if kind=='numeric':
        q.update(unit='units',open_lower_bound=False,open_upper_bound=False,inbound_outcome_count=200,
          scaling=dict(range_min=-3,range_max=3,continuous_range=[-3+6*i/200 for i in range(201)],zero_point=None,
                       open_lower_bound=False,open_upper_bound=False,inbound_outcome_count=200))
    return dict(id=101,status='open',projects={'tournament':[{'id':33121}]},question=q)
def binding(post):return bind_post(post,ME,tournament_id=33121,expected_username='Frogman-bot',now=NOW)
def candidate(b,d):
    doc=extract('https://example.org/source',dict(type='text',data='The supplied historical observation was 19 units.'),NOW.isoformat())
    p=payload(b['question'],[doc],NOW.isoformat())
    a=dict(question_id=b['question']['id'],decision='forecast',distribution=d,
           facts=[dict(claim='The supplied historical observation was 19 units.',url=doc['url'],observation_date=None)],
           assumptions=['Subjective future judgement.'],gaps=[],rationale='The supplied baseline supports a modest increase, with uncertainty about the future.')
    return dict(status='candidate',question_sha256=digest(b['question']),raw_answer=a,forecast_request=p,
      review=dict(decision='pass',question_sha256=digest(b['question']),original_answer_sha256=digest(a),usable_answer=copy.deepcopy(a),documents_sha256=digest(audit_documents(p))))
def make_plan(post=None):
    post=post or post_fixture();b=binding(post)
    if post['question']['type']=='binary':d=dict(kind='binary',probability=.55)
    elif post['question']['type']=='multiple_choice':d=dict(kind='categorical',probabilities={'Active A':.6,'Active B':.4})
    else:d=dict(kind='normal_mixture',components=[dict(weight=1,mean=0,sd=1)],lower_bound=-3,upper_bound=3)
    return prepare(b,candidate(b,d))
def with_forecast(post,plan):
    p=copy.deepcopy(post);p['question']['my_forecasts']['latest']=dict(author_id=500,question_id=102,start_time=NOW.timestamp()+10,end_time=None,forecast_values=plan['expected_forecast_values'])
    return p

class AdapterTests(unittest.TestCase):
    def test_post_question_ids_and_spot_cutoff_are_distinct(self):
        b=binding(post_fixture());p=make_plan()
        self.assertEqual(p['payload'][0]['question'],102);self.assertEqual(p['comment']['on_post'],101)
        self.assertEqual(b['cutoff_utc'],'2026-10-16T00:00:00+00:00')
        self.assertFalse(p['submission_enabled']);self.assertTrue(p['comment']['is_private'])
    def test_wrong_account_tournament_masking_and_existing_forecast_stop(self):
        for change in ('tournament','masked','already','spot','group'):
            p=post_fixture()
            if change=='tournament':p['projects']['tournament']=[]
            elif change=='masked':p['question']['resolution_criteria']=None
            elif change=='already':p['question']['my_forecasts']['latest']={'forecast_values':[.5,.5]}
            elif change=='spot':p['question']['spot_scoring_time']='2026-10-15T17:00:00Z'
            else:p['group_of_questions']={'id':1}
            with self.assertRaises((Stop,ValueError)):binding(p)
        with self.assertRaises(Stop):bind_post(post_fixture(),dict(ME,username='other'),tournament_id=33121,expected_username='Frogman-bot',now=NOW)
    def test_mc_option_history_is_mapped_without_inventing_inactive_mass(self):
        p=make_plan(post_fixture('multiple_choice'))
        self.assertEqual(p['expected_forecast_values'],[None,.4,.6])
        self.assertEqual(set(p['payload'][0]['probability_yes_per_category']),{'Active A','Active B'})
    def test_numeric_uses_provided_grid_and_logs_concentrated_projection(self):
        post=post_fixture('numeric');b=binding(post);p=make_plan(post)
        self.assertEqual(len(p['expected_forecast_values']),201)
        self.assertEqual(p['expected_forecast_values'][0],0);self.assertEqual(p['expected_forecast_values'][-1],1)
        self.assertAlmostEqual(p['expected_forecast_values'][100],.5)
        a=candidate(b,dict(kind='normal_mixture',components=[dict(weight=1,mean=0,sd=.01)],lower_bound=-3,upper_bound=3))
        original=copy.deepcopy(a);projected=prepare(b,a)
        self.assertEqual(a,original);self.assertGreater(projected['format_adaptation']['pmf_l1'],0)
        self.assertGreater(len(projected['expected_forecast_values']),0)
    def test_readback_checks_author_values_time_and_contract(self):
        p=make_plan();post=with_forecast(post_fixture(),p)
        self.assertTrue(verify_readback(p,post,ME)['verified'])
        for field,value in [('author_id',999),('question_id',101),('start_time',NOW.timestamp()-100),('forecast_values',[.5,.5])]:
            bad=copy.deepcopy(post);bad['question']['my_forecasts']['latest'][field]=value
            with self.assertRaises(Stop):verify_readback(p,bad,ME)
        bad=copy.deepcopy(post);bad['question']['fine_print']='Changed criteria.'
        with self.assertRaises(Stop):verify_readback(p,bad,ME)
        bad=copy.deepcopy(p);bad['payload'][0]['probability_yes']=.7
        with self.assertRaises(Stop):verify_readback(bad,post,ME)
    def test_default_disabled_without_any_transport_or_db_write(self):
        calls=[]
        with self.assertRaises(Stop):submit_once(make_plan(),None,fetch=calls.append,post_forecast=calls.append,clock=lambda:NOW)
        self.assertEqual(calls,[])
    def test_timeout_after_server_acceptance_is_read_back_without_retry(self):
        p=make_plan();post=post_fixture();calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'state.db',cap_usd='2',period_key='test')
            def fetch(pid):return ME,post
            def send(payload):
                calls.append(payload);post.update(with_forecast(post,p));raise TimeoutError()
            try:
                r=submit_once(p,ledger,fetch=fetch,post_forecast=send,clock=lambda:NOW,enabled=True)
                self.assertTrue(r['verified']);self.assertEqual(len(calls),1)
                with self.assertRaises(Stop):submit_once(p,ledger,fetch=fetch,post_forecast=send,clock=lambda:NOW,enabled=True)
                self.assertEqual(len(calls),1)
            finally:ledger.close()
    def test_uncertain_submission_survives_restart_and_can_only_be_reconciled(self):
        p=make_plan();post=post_fixture();calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.db';l=Ledger(path,cap_usd='2',period_key='test')
            try:
                with self.assertRaises(Stop):submit_once(p,l,fetch=lambda pid:(ME,post),post_forecast=calls.append,clock=lambda:NOW,enabled=True)
            finally:l.close()
            l=Ledger(path,cap_usd='2',period_key='test')
            try:
                with self.assertRaises(GlobalStop):submit_once(p,l,fetch=lambda pid:(ME,post),post_forecast=calls.append,clock=lambda:NOW,enabled=True)
                self.assertEqual(len(calls),1)
                r=reconcile(p,l,fetch=lambda pid:(ME,with_forecast(post,p)))
                self.assertTrue(r['verified'])
            finally:l.close()

if __name__=='__main__':unittest.main()
