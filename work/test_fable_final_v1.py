"""Regression for the review prompt/validator alias contract (Fable N4)."""
import copy,json,unittest
from bounded_evidence import extract
import candidate_profile_v1 as profile
from test_candidate_pipeline_v1 import Q,TIME,OBS,response

class FableFinalTests(unittest.TestCase):
    def setUp(self):
        docs=[extract('https://example.org/'+str(i),dict(type='text',data=OBS),TIME.isoformat()) for i in range(2)]
        self.docs=profile.audit_documents(profile.payload(Q,docs,TIME.isoformat()))
        self.answer=dict(question_id=Q['id'],decision='forecast',distribution=dict(kind='binary',probability=.55),
            facts=[dict(claim=OBS,url=docs[1]['url'],observation_date=None)],assumptions=['Future judgement.'],gaps=[],rationale='Future forecast.')
        self.body=dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url=docs[1]['url'],quote=OBS)])],issues=[],subjective=[])
    def test_prompt_distinguishes_fact_url_from_issue_aliases(self):
        p=profile.review_payload(Q,self.answer,self.docs)
        self.assertIn("For checks.evidence, use exactly the fact's own URL",p['system'])
        self.assertIn('For issues.evidence, you may cite any supplied document URL or listed alias URL',p['system'])
        self.assertNotIn('Quotes may cite the original URL or any listed alias URL',p['system'])
        self.assertEqual(len(json.loads(p['messages'][0]['content'])['documents']),1)
    def test_fact_original_url_works_even_when_it_is_a_group_alias(self):
        r=profile.review_check(response('compact_review',self.body),Q,self.answer,self.docs)
        self.assertEqual(r['decision'],'pass')
        swapped=copy.deepcopy(self.body);swapped['checks'][0]['evidence'][0]['url']='https://example.org/0'
        r=profile.review_check(response('compact_review',swapped),Q,self.answer,self.docs)
        self.assertEqual(r['decision'],'unavailable')
        self.assertIn('Invalid fact checks',r['technical_error'])
    def test_issue_evidence_can_use_other_supplied_alias_without_changing_fact_url(self):
        b=copy.deepcopy(self.body)
        b['issues']=[dict(section='assumptions',index=0,claim='Future judgement.',kind='future_judgement',material=False,
            reason='A future choice, not an asserted historical rate.',evidence=[dict(url='https://example.org/0',quote=OBS)])]
        r=profile.review_check(response('compact_review',b),Q,self.answer,self.docs)
        self.assertEqual(r['eligibility'],'allow');self.assertNotEqual(r['decision'],'unavailable')
        self.assertEqual(r['labels'],['future_judgement'])

if __name__=='__main__':unittest.main()
