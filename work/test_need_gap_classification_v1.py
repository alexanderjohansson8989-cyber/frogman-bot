import copy,json,unittest
import test_candidate_pipeline_v1 as fixtures
from test_candidate_pipeline_v1 import response
from research_assessment_v1 import parse
from need_gap_classification_v1 import CONTRACT,KINDS
from independent_runner import Stop
import candidate_profile_v1 as profile

class GapTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.AssessmentTests();f.setUp();self.req=f.req;self.finding=f.f
    def run_parse(self,labels,findings=None,request=None,stop='end_turn'):
        return parse(response('research_quality',dict(findings=findings or [],
            selected_urls=['https://example.org/child'],missing_needs=labels),stop),request or self.req)
    def test_each_label_is_diagnostic_and_never_clears_missing(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                r=self.run_parse([dict(need_id='n1',gap_kind=kind)])
                self.assertEqual(r['version'],CONTRACT)
                self.assertEqual(r['ledger']['required_observation_needs_missing'],['n1'])
                self.assertEqual(r['ledger']['covered_observation_need_ids'],[])
                label=r['ledger']['missing_need_classifications'][0]
                self.assertEqual(label['gap_kind'],kind);self.assertEqual(label['classification_status'],'model_reported')
                self.assertTrue(label['semantic_review_pending'])
    def test_bad_labels_preserve_links_and_become_unknown(self):
        cases=[None,'invented',{},[dict(need_id='n1',gap_kind='search_more')],
            [dict(need_id='n1',gap_kind='future_release')]*2,
            [dict(need_id='n1',gap_kind='future_release',extra='untrusted')],
            [dict(need_id='n99',gap_kind='future_release')],
            [dict(need_id='n1',gap_kind=['unknown'])],
            [dict(need_id='n1',gap_kind='future_release')]*6]
        for labels in cases:
            with self.subTest(labels=labels):
                r=self.run_parse(labels)
                self.assertEqual(r['selected_urls'],['https://example.org/child'])
                self.assertEqual(r['ledger']['missing_need_classifications'][0]['gap_kind'],'unknown')
    def test_covered_need_cannot_be_relabelled_missing(self):
        r=self.run_parse([dict(need_id='n1',gap_kind='missing_observation')],[self.finding])
        self.assertEqual(len(r['ledger']['findings']),1)
        self.assertEqual(r['ledger']['required_observation_needs_missing'],[])
        self.assertEqual(r['ledger']['missing_need_classifications'],[])
    def test_legacy_response_still_works_but_unknown_is_explicit(self):
        raw=response('research_quality',dict(findings=[],selected_urls=[]))
        r=parse(raw,self.req)
        self.assertEqual(r['ledger']['missing_need_classifications'][0]['classification_status'],'not_reported')
        legacy=copy.deepcopy(self.req);body=json.loads(legacy['messages'][0]['content'])
        body.pop('assessment_contract');legacy['messages'][0]['content']=json.dumps(body)
        old=parse(raw,legacy)
        self.assertEqual(old['version'],'research-assessment-v2')
        self.assertNotIn('missing_need_classifications',old['ledger'])
        with self.assertRaises(Stop):self.run_parse([],request=legacy)
    def test_truncation_unknown_top_field_and_invalid_quote_stay_invalid(self):
        with self.assertRaises(Stop):self.run_parse([],stop='max_tokens')
        raw=response('research_quality',dict(findings=[],selected_urls=[],missing_needs=[],new_search=True))
        with self.assertRaises(Stop):parse(raw,self.req)
        bad=dict(self.finding,quote='An invented quotation which is not in the text.')
        r=self.run_parse([dict(need_id='n1',gap_kind='future_release')],[bad])
        self.assertFalse(r['ledger']['findings'])
        self.assertEqual(r['ledger']['required_observation_needs_missing'],['n1'])
    def test_multiple_needs_host_coverage_decides_labels(self):
        req=copy.deepcopy(self.req);body=json.loads(req['messages'][0]['content'])
        body['needs'] += [dict(id='n2',description='Future target',required=True),dict(id='n3',description='Optional',required=False)]
        req['messages'][0]['content']=json.dumps(body)
        r=self.run_parse([dict(need_id='n2',gap_kind='future_uncertainty'),dict(need_id='n3',gap_kind='missing_observation')],[self.finding],req)
        self.assertEqual(r['ledger']['required_observation_needs_missing'],['n2'])
        self.assertEqual([x['need_id'] for x in r['ledger']['missing_need_classifications']],['n2'])
    def test_pipeline_has_one_need_and_no_gap_evidence_in_forecast(self):
        result,calls,_,saved=fixtures.PipelineTests().execute()
        self.assertEqual([s for s,p in calls],['search','need','forecast','review'])
        self.assertEqual(profile.STAGE_LIMITS['need'],1)
        need=next(p for s,p in calls if s=='need')
        self.assertEqual(need['output_config'],{'effort':'medium'})
        self.assertEqual(need['max_tokens'],6000)
        self.assertIn('findings, selected_urls and missing_needs.',need['system'])
        self.assertNotIn('findings and selected_urls.',need['system'])
        self.assertTrue(result['assessments'][0]['ledger']['missing_need_classifications'])
        self.assertTrue(any(s['assessments'] for s in saved))
        body=json.loads(result['forecast_request']['messages'][0]['content'])
        self.assertNotIn('assessment_contract',body)
        self.assertNotIn('missing_need_classifications',body)

if __name__=='__main__':unittest.main()
