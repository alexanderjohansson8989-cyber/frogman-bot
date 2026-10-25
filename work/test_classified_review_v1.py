import copy,json,tempfile,unittest
from pathlib import Path
from classified_review_v1 import evaluate,eligible,VERSION
from operations_ledger_v1 import Ledger
from competition_adapter_v1 import prepare
from competition_adapter_v1 import short_comment_rationale
from independent_runner import Stop
import test_candidate_service_v1 as service_fixture
OBS=service_fixture.OBS
from test_competition_adapter_v1 import binding,post_fixture,candidate

URL='https://example.org/source'

def response(body,stop='end_turn'):
    return dict(stop_reason=stop,content=[dict(type='text',text='<compact_review>'+json.dumps(body)+'</compact_review>')])

class ClassifiedReviewTests(unittest.TestCase):
    def setUp(self):
        self.q={'criteria':'Use the specified future observation.'}
        self.a=dict(facts=[dict(claim=OBS,url=URL)],assumptions=['Future judgement.'],gaps=['One context series is unavailable.'],rationale='A forecast about the future.',distribution=dict(kind='binary',probability=.55))
        self.docs=[dict(url=URL,text=OBS)]
        self.body=dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url=URL,quote=OBS)])],issues=[],subjective=[])
    def issue(self,kind='missing_evidence',material=True,section='facts',index=0):
        claim=self.a[section] if section=='rationale' else self.a[section][index]['claim'] if section=='facts' else self.a[section][index]
        return dict(kind=kind,material=material,section=section,index=index,claim=claim,reason='Specific supplied input limitation affecting this premise.',evidence=[])
    def check(self,body=None,stop='end_turn'):
        return evaluate(response(body or self.body,stop),self.q,self.a,self.docs)
    def test_supported_pass_and_future_judgement_do_not_change_distribution(self):
        before=copy.deepcopy(self.a);self.body['issues']=[self.issue('future_judgement',section='assumptions')]
        r=self.check();self.assertEqual(r['decision'],'pass');self.assertEqual(r['usable_answer'],before)
        self.assertTrue(eligible(r,self.q,self.a,self.docs));self.assertEqual(self.a,before)
    def test_missing_empirical_support_is_qualified_not_semantic_pass(self):
        self.body['checks'][0].update(supported=False,evidence=[])
        self.body['issues']=[self.issue()]
        r=self.check();self.assertEqual(r['decision'],'qualified');self.assertEqual(r['labels'],['unsupported_evidence'])
        self.assertEqual(r['usable_answer'],self.a)
    def test_material_hard_issues_hold_all_three_classes(self):
        for kind in ('explicit_contradiction','target_mismatch','derivation_error'):
            self.body['issues']=[self.issue(kind)]
            r=self.check();self.assertEqual(r['eligibility'],'hold');self.assertIsNone(r['usable_answer'])
            self.assertFalse(eligible(r,self.q,self.a,self.docs))
    def test_incidental_issue_does_not_hold_forecast(self):
        self.body['issues']=[self.issue('explicit_contradiction',False)]
        r=self.check();self.assertEqual(r['decision'],'qualified');self.assertEqual(r['labels'],['incidental_issue'])
    def test_mixed_missing_and_material_error_still_holds(self):
        self.body['issues']=[self.issue(),self.issue('target_mismatch')]
        self.assertEqual(self.check()['decision'],'reject')
    def test_changed_claim_unseen_quote_and_missing_fact_class_are_technical(self):
        bodies=[]
        b=copy.deepcopy(self.body);b['issues']=[dict(self.issue(),claim='Invented stronger claim')];bodies.append(b)
        b=copy.deepcopy(self.body);b['checks'][0]['evidence'][0]['quote']='This sentence is not in the actual source.';bodies.append(b)
        b=copy.deepcopy(self.body);b['checks'][0].update(supported=False,evidence=[]);bodies.append(b)
        for b in bodies:
            r=self.check(b);self.assertEqual(r['decision'],'unavailable');self.assertEqual(r['labels'],['review_unavailable'])
    def test_truncation_and_invalid_schema_keep_raw_but_never_claim_pass(self):
        for reply in (response(self.body,'max_tokens'),response({'wrong':True}),None):
            r=evaluate(reply,self.q,self.a,self.docs)
            self.assertEqual(r['decision'],'unavailable');self.assertEqual(r['usable_answer'],self.a)
    def test_forged_disposition_or_changed_inputs_fail_revalidation(self):
        r=self.check();r['labels']=['unsupported_evidence'];self.assertFalse(eligible(r,self.q,self.a,self.docs))
        r=self.check();self.assertFalse(eligible(r,{'criteria':'Different'},self.a,self.docs))
        self.assertFalse(eligible(r,self.q,self.a,[dict(url=URL,text='Different source')]))
        self.body['issues']=[self.issue('target_mismatch')];r=self.check();r.update(decision='pass',eligibility='allow',usable_answer=self.a)
        self.assertFalse(eligible(r,self.q,self.a,self.docs))
    def test_legacy_free_text_objections_are_not_relabelled_as_missing_evidence(self):
        b=dict(checks=self.body['checks'],unsupported=['A historical premise is unsupported.'],subjective=[])
        r=self.check(b);self.assertEqual(r['decision'],'reject');self.assertEqual(r['labels'],['legacy_unclassified_reject'])
    def test_existing_numeric_and_question_contracts_remain_gates(self):
        b=binding(post_fixture());r=candidate(b,dict(kind='binary',probability=1))
        docs=__import__('candidate_profile_v1').audit_documents(r['forecast_request'])
        r['review']=evaluate(response({},'max_tokens'),b['question'],r['raw_answer'],docs)
        with self.assertRaises(Stop):prepare(b,r)
    def test_long_comment_is_excerpted_without_mutating_forecast(self):
        b=binding(post_fixture());r=candidate(b,dict(kind='binary',probability=.55))
        r['raw_answer']['rationale']='A complete forecast explanation. '+('Additional conditional detail with uncertainty. '*30)
        before=copy.deepcopy(r['raw_answer'])
        docs=__import__('candidate_profile_v1').audit_documents(r['forecast_request'])
        r['review']=evaluate(response({},'max_tokens'),b['question'],r['raw_answer'],docs)
        plan=prepare(b,r)
        self.assertEqual(r['raw_answer'],before);self.assertEqual(plan['payload'][0]['probability_yes'],.55)
        self.assertEqual(plan['comment_adaptation']['method'],'labelled-prefix-excerpt-v1')
        self.assertTrue(plan['comment']['text'].startswith('Rationale excerpt: '))
        self.assertLessEqual(plan['comment_adaptation']['comment_characters'],650)
    def test_short_comment_unchanged_and_invalid_comment_not_repaired(self):
        self.assertEqual(short_comment_rationale('An adequate rationale.'),('An adequate rationale.',None))
        for text in ('too short','x'*4001,None):
            with self.assertRaises(Stop):short_comment_rationale(text)

class ClassifiedServiceTests(unittest.TestCase):
    def execute(self,body=None,timeout=False):
        calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'test.sqlite3',cap_usd='3',period_key='test')
            try:
                result,_=service_fixture.ServiceTests().execute(ledger,tmp,calls,review_body=body,review_timeout=timeout)
                states=ledger.db.execute('SELECT state FROM operations').fetchall()
                return result,calls,states
            finally:ledger.close()
    def missing(self):
        return dict(checks=[dict(fact_index=0,supported=False,evidence=[])],issues=[dict(section='facts',index=0,claim=OBS,kind='missing_evidence',material=True,reason='The supplied excerpt lacks the needed context.',evidence=[])],subjective=[])
    def test_missing_evidence_reaches_disabled_plan_with_label_and_unchanged_probability(self):
        r,calls,states=self.execute(self.missing())
        self.assertEqual(r['status'],'prepared_local_only');self.assertEqual(len(calls),4)
        p=r['submission_plan'];self.assertEqual(p['review_labels'],['unsupported_evidence'])
        self.assertEqual(p['review_decision'],'qualified');self.assertEqual(p['payload'][0]['probability_yes'],.55)
        self.assertFalse(p['submission_enabled']);self.assertEqual(states,[('settled',)]*4)
    def test_technical_review_only_reaches_labelled_disabled_plan(self):
        r,_,_=self.execute({'invalid':'schema'})
        self.assertEqual(r['status'],'prepared_local_only');self.assertEqual(r['submission_plan']['review_labels'],['review_unavailable'])
    def test_material_target_error_blocks_preparation(self):
        body=self.missing();body['issues'][0]['kind']='target_mismatch'
        r,_,_=self.execute(body);self.assertEqual(r['status'],'rejected');self.assertNotIn('submission_plan',r)
    def test_uncertain_review_transport_never_uses_technical_fallback(self):
        r,calls,states=self.execute(timeout=True)
        self.assertEqual(r['status'],'global_stop');self.assertNotIn('submission_plan',r)
        self.assertEqual(len(calls),4);self.assertEqual(states[-1],('uncertain',))

if __name__=='__main__':unittest.main()
