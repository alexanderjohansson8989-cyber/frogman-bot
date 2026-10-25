"""Follow-up regression: null opening, claim whitespace, uncertain readback."""
import copy,datetime as dt,tempfile,unittest
from pathlib import Path
from independent_runner import Stop
from operations_ledger_v1 import Ledger,GlobalStop
from competition_adapter_v1 import bind_test_post,reconcile,submit_once
from candidate_api_v1 import TransportOutcomeUnknown
from classified_review_v1 import evaluate,eligible
import test_classified_review_v1 as rf
from test_competition_adapter_v1 import binding,post_fixture,make_plan,ME,NOW
from candidate_comments_v1 import readback

class FollowupTests(unittest.TestCase):
    def test_missing_or_null_opening_stops_both_binding_routes(self):
        for missing in (False,True):
            p=post_fixture()
            if missing:del p['question']['open_time']
            else:p['question']['open_time']=None
            for call in (lambda:binding(p),lambda:bind_test_post(p,ME,listing_post_ids=[p['id']],now=NOW)):
                with self.assertRaisesRegex(Stop,'Opening time required'):call()
    def test_equivalent_opening_offsets_and_actual_microsecond_changes(self):
        p=post_fixture();original=binding(p)
        p['question']['open_time']='2026-10-14T02:00:00+02:00'
        self.assertEqual(binding(p)['question_contract_sha256'],original['question_contract_sha256'])
        p['question']['actual_close_time']='2026-10-17T00:00:00.000001Z'
        self.assertNotEqual(binding(p)['question_contract_sha256'],original['question_contract_sha256'])
    def case(self,section,original,claim):
        f=rf.ClassifiedReviewTests();f.setUp()
        if section=='rationale':f.a[section]=original;index=None
        elif section=='facts':f.a['facts'][0]['claim']=original;index=0
        else:f.a[section]=[original];index=0
        b=copy.deepcopy(f.body)
        b['issues']=[dict(f.issue('target_mismatch',section=section,index=index),claim=claim)]
        return evaluate(rf.response(b),f.q,f.a,f.docs),f
    def test_whitespace_only_claims_preserve_hard_issue_and_original_offsets(self):
        for section in ('facts','assumptions','gaps','rationale'):
            original='The baseline\n  was 19 units.'
            r,f=self.case(section,original,'The baseline was 19 units.')
            self.assertEqual(r['eligibility'],'hold',section)
            pos=r['claim_bindings'][0]['positions'][0]
            self.assertEqual(original[pos['start']:pos['end']],original)
            self.assertEqual(r['original_answer'],f.a)
            self.assertFalse(eligible(r,f.q,f.a,f.docs))
    def test_whitespace_partial_rationale_and_repetition(self):
        claim='A relevant baseline is 19 units.'
        original='Context. A relevant\n baseline is 19 units. '+claim
        r,_=self.case('rationale',original,claim)
        self.assertEqual(r['eligibility'],'hold')
        positions=r['claim_bindings'][0]['positions'];self.assertEqual(len(positions),2)
        self.assertEqual(positions[0]['original_quote'],'A relevant\n baseline is 19 units.')
    def test_changed_number_word_case_punctuation_and_short_substring_fail(self):
        original='The baseline\n was 19 units.'
        for claim in ('The baseline was 20 units.','The forecast was 19 units.',
                      'the baseline was 19 units.','The baseline was 19 units!',
                      'was 19 units.'):
            r,_=self.case('rationale',original,claim)
            self.assertEqual(r['decision'],'unavailable',claim)
    def test_full_field_cannot_be_replaced_by_substring(self):
        r,_=self.case('assumptions','Context. The baseline\n was 19 units.','The baseline was 19 units.')
        self.assertEqual(r['decision'],'unavailable')
    def test_forecast_absent_after_timeout_is_global_and_not_retried(self):
        plan=make_plan();writes=[];reads=[]
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'db',cap_usd='3',period_key='test')
            def post(p):writes.append(p);raise TransportOutcomeUnknown('Simulated timeout')
            def fetch(pid):reads.append(pid);return ME,post_fixture()
            try:
                with self.assertRaises(GlobalStop):
                    submit_once(plan,ledger,fetch=fetch,post_forecast=post,clock=lambda:NOW,enabled=True)
                self.assertEqual((len(writes),len(reads)),(1,2))
                self.assertEqual(ledger.db.execute('SELECT state FROM submissions').fetchone()[0],'uncertain')
                with self.assertRaises(GlobalStop):ledger.guard()
            finally:ledger.close()
    def test_malformed_forecast_readback_is_also_global(self):
        plan=make_plan()
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'db',cap_usd='3',period_key='test')
            ledger.prepare_submission('500/33121/102',plan);ledger.begin_submission('500/33121/102',plan)
            try:
                with self.assertRaises(GlobalStop):reconcile(plan,ledger,fetch=lambda _: (ME,{}))
                self.assertEqual(ledger.db.execute('SELECT state FROM submissions').fetchone()[0],'uncertain')
            finally:ledger.close()
    def test_comment_absent_or_malformed_is_global_while_uncertain(self):
        from types import SimpleNamespace
        for page in ({'results':[],'next':None}, {'wrong':True}):
            with tempfile.TemporaryDirectory() as tmp:
                ledger=Ledger(Path(tmp)/'db',cap_usd='3',period_key='test')
                plan=dict(key='500/33121/102/comment',forecast_plan=make_plan(),payload={},prepared_utc=NOW.isoformat())
                ledger.prepare_submission(plan['key'],plan);ledger.begin_submission(plan['key'],plan)
                try:
                    with self.assertRaises(GlobalStop):readback(plan,ledger,SimpleNamespace(comments=lambda *a:page))
                    self.assertEqual(ledger.db.execute('SELECT state FROM submissions').fetchone()[0],'uncertain')
                finally:ledger.close()
    def test_unregistered_pure_readback_error_does_not_claim_uncertain_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'db',cap_usd='3',period_key='test')
            try:
                with self.assertRaises(Stop) as error:reconcile(make_plan(),ledger,fetch=lambda _: (ME,post_fixture()))
                self.assertNotIsInstance(error.exception,GlobalStop)
            finally:ledger.close()

if __name__=='__main__':unittest.main()
