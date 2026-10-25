import copy,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from outcome_metrics_v1 import question_metrics,queue_metrics
from operations_ledger_v1 import Ledger
from independent_runner import Stop
import test_candidate_service_v1 as service
import test_classified_review_v1 as review_fixture
import test_production_cycle_v1 as cycle_fixture


class OutcomeMetricsTests(unittest.TestCase):
    def execute(self,**kw):
        calls=[]
        with tempfile.TemporaryDirectory() as folder:
            ledger=Ledger(Path(folder)/'state.db',cap_usd='3',period_key='metrics-test')
            try:
                r,saved=service.ServiceTests().execute(ledger,folder,calls,**kw)
                session=ledger.db.execute('SELECT status FROM sessions').fetchone()[0]
                return r,saved,calls,session
            finally:ledger.close()

    def test_success_metrics_persist_with_final_result_and_do_not_add_calls(self):
        r,saved,calls,session=self.execute();m=r['outcome_metrics']
        self.assertEqual(r['status'],'prepared_local_only');self.assertEqual(session,'complete')
        self.assertTrue(m['raw_forecast']);self.assertFalse(m['review_hold'])
        self.assertEqual(m['review_decision'],'pass');self.assertEqual(m['preparation'],'prepared')
        self.assertEqual(m['captured_document_records'],1);self.assertIsNone(m['source_attempts'])
        self.assertEqual(saved[-1]['outcome_metrics'],m);self.assertEqual(len(calls),4)
        self.assertNotIn('probability',str(m));self.assertNotIn(service.OBS,str(m))

    def test_real_review_classifications_are_distinct(self):
        f=review_fixture.ClassifiedReviewTests();f.setUp()
        cases=[]
        reject=copy.deepcopy(f.body);reject['issues']=[f.issue('target_mismatch')]
        qualified=copy.deepcopy(f.body);qualified['checks'][0].update(supported=False,evidence=[])
        qualified['issues']=[f.issue()]
        cases.extend([(reject,'reject',True,'not_reached'),(qualified,'qualified',False,'prepared'),
                      ({'bad':'shape'},'unavailable',False,'prepared')])
        for body,decision,hold,preparation in cases:
            with self.subTest(decision=decision):
                r,_,calls,_=self.execute(review_body=body);m=r['outcome_metrics']
                self.assertEqual((m['review_decision'],m['review_hold'],m['preparation']),
                                 (decision,hold,preparation));self.assertEqual(len(calls),4)

    def test_transport_uncertainty_stays_global_and_active(self):
        r,_,calls,session=self.execute(review_timeout=True)
        self.assertEqual(r['status'],'global_stop');self.assertEqual(session,'active')
        self.assertEqual(r['outcome_metrics']['review_decision'],'not_reached')
        self.assertTrue(r['outcome_metrics']['raw_forecast']);self.assertEqual(len(calls),4)

    def test_prepare_failure_not_confused_with_review_hold(self):
        with patch('candidate_service_v1.prepare',side_effect=Stop('Synthetic format failure')):
            r,_,_,_=self.execute()
        m=r['outcome_metrics'];self.assertEqual(m['preparation'],'failed')
        self.assertEqual(m['review_decision'],'pass');self.assertFalse(m['review_hold'])
        self.assertEqual(m['diagnostic_stages']['prepare'],1)

    def test_capture_and_abstention_are_not_raw_forecast_denominators(self):
        r=dict(status='validation_stop',raw_answer=None,initial_capture=dict(documents=[],
            failures=[{'error_type':'HTTPError'}],source_attempts=1),documents=[],
            diagnostics=[dict(stage='pipeline',error_type='Stop')])
        before=copy.deepcopy(r);m=question_metrics(r)
        self.assertEqual(r,before);self.assertFalse(m['raw_forecast'])
        self.assertEqual((m['source_attempts'],m['capture_failure_records']), (1,1))
        r=dict(status='abstain',raw_answer=dict(decision='abstain'))
        self.assertTrue(question_metrics(r)['model_abstained'])

    def test_aggregate_denominator_missing_metrics_and_no_false_quality_pass(self):
        rows=[]
        for decision,eligibility in [('pass','allow'),('qualified','allow'),('unavailable','allow'),('reject','hold')]:
            result=dict(status='candidate',raw_answer=dict(decision='forecast'),
                        review=dict(decision=decision,eligibility=eligibility,labels=[decision]))
            rows.append(dict(status='candidate',outcome_metrics=question_metrics(result)))
        rows.append(dict(status='abstain',outcome_metrics=question_metrics(dict(status='abstain',raw_answer=dict(decision='abstain')))))
        rows.append(dict(status='historical_without_details'))
        queue=dict(attempted=rows,skipped=[dict(reason='duplicate_intake')],stop_reason='attempt_limit')
        before=copy.deepcopy(queue);m=queue_metrics(queue,intake_records=8,eligible_new_questions=7)
        self.assertEqual(queue,before);self.assertEqual(m['raw_forecasts'],4)
        self.assertEqual(m['hold_fraction_among_raw_forecasts'],.25)
        self.assertEqual(m['review_decisions_among_raw_forecasts'],{'pass':1,'qualified':1,'unavailable':1,'reject':1})
        self.assertEqual(m['attempts_without_metrics'],1);self.assertEqual(m['model_abstentions'],1)
        self.assertIsNone(queue_metrics(dict(attempted=[],skipped=[],stop_reason=None),intake_records=0,
                                       eligible_new_questions=0)['hold_fraction_among_raw_forecasts'])

    def test_publication_keeps_service_metrics_and_restart_does_not_double_count(self):
        f=cycle_fixture.CycleTests();f.setUp();f.config['submit']=True
        with tempfile.TemporaryDirectory() as folder:
            first=f.run_cycle(Path(folder)/'first');second=f.run_cycle(Path(folder)/'second')
        self.assertEqual(first['queue']['attempted'][0]['status'],'submitted_verified')
        self.assertEqual(first['queue']['outcome_metrics']['raw_forecasts'],1)
        self.assertEqual(first['queue']['attempted'][0]['outcome_metrics']['preparation'],'prepared')
        self.assertEqual(second['queue']['outcome_metrics']['attempted_questions'],0)
        self.assertIsNone(second['queue']['outcome_metrics']['hold_fraction_among_raw_forecasts'])
        self.assertEqual(f.model.model.calls,4);self.assertEqual(len(f.server.comments),1)


if __name__=='__main__':unittest.main()
