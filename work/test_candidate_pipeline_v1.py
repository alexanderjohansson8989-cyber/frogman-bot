import copy,datetime as dt,json,tempfile,unittest
from pathlib import Path
from bounded_evidence import extract
from independent_runner import Stop
from research_assessment_v1 import parse
from candidate_pipeline_v1 import run_question
import candidate_profile_v1 as profile

TIME=dt.datetime(2026,10,15,18,tzinfo=dt.timezone.utc)
Q=dict(id='new_question',type='binary',question='Will the future level increase?',criteria='Compare the same series, strictly higher.')
OBS='The August observed level was 19 units.'

def response(tag,a,stop='end_turn'):
    return dict(stop_reason=stop,content=[dict(type='text',text='<'+tag+'>'+json.dumps(a)+'</'+tag+'>')])

class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.doc=extract('https://example.org/source',dict(type='text',data=OBS),TIME.isoformat())
        self.needs=[dict(id='n1',description='Find the observed level.',required=True)]
        self.req=profile.need_payload(Q,[self.doc],TIME.isoformat(),self.needs,[dict(url='https://example.org/child')])
        self.f=dict(need_id='n1',source_sha256=self.doc['original_sha256'],quote=OBS,
            kind='observation',series='test',unit='units',period='August',target_relevance='baseline',vintage_status='consistent')
    def call(self,f=None,selected=None,raw=None,request=None):
        return parse(raw or response('research_quality',dict(findings=[self.f if f is None else f],
            selected_urls=['https://example.org/child'] if selected is None else selected)),request or self.req)
    def test_bad_quote_preserves_valid_links_and_reports_missing_need(self):
        f=dict(self.f,quote='The August observed level was 99 units.')
        r=self.call(f)
        self.assertEqual(r['selected_urls'],['https://example.org/child'])
        self.assertEqual(r['ledger']['required_observation_needs_missing'],['n1'])
        self.assertEqual(r['diagnostics'][0]['scope'],'finding')
    def test_sent_span_only_never_unseen_original(self):
        b=json.loads(self.req['messages'][0]['content']);b['sources'][0]['spans'][0]['text']='Other unrelated supplied content.'
        p=copy.deepcopy(self.req);p['messages'][0]['content']=json.dumps(b)
        self.assertFalse(self.call(request=p)['ledger']['findings'])
    def test_duplicate_contexts_and_aliases_have_explicit_status(self):
        b=json.loads(self.req['messages'][0]['content']);b['sources'][0]['spans'][0]['text']=OBS+' '+OBS
        p=copy.deepcopy(self.req);p['messages'][0]['content']=json.dumps(b)
        r=self.call(request=p);f=r['ledger']['findings'][0]
        self.assertEqual(len(f['positions']),2);self.assertTrue(f['context_selection_pending'])
        self.assertEqual(r['ledger']['required_observation_needs_missing'],['n1'])
        b['sources'][0]['spans'][0]['text']=OBS;b['sources'].append(copy.deepcopy(b['sources'][0]))
        p['messages'][0]['content']=json.dumps(b);r=self.call(request=p)
        self.assertEqual(len(r['ledger']['findings'][0]['positions']),1)
        self.assertEqual(r['ledger']['required_observation_needs_missing'],[])
    def test_unlisted_links_do_not_destroy_valid_evidence(self):
        r=self.call(selected=['https://invented.example/'])
        self.assertEqual(r['selected_urls'],[]);self.assertEqual(len(r['ledger']['findings']),1)
    def test_incomplete_response_never_rescued(self):
        a=dict(findings=[self.f],selected_urls=['https://example.org/child'])
        with self.assertRaises(Stop):self.call(raw=response('research_quality',a,'max_tokens'))
    def test_complete_bare_json_has_identical_evidence_checks(self):
        a=dict(findings=[self.f],selected_urls=['https://example.org/child'])
        r=dict(stop_reason='end_turn',content=[dict(type='text',text=json.dumps(a))])
        self.assertEqual(self.call(raw=r),self.call())
        a['findings'][0]=dict(self.f,quote='An invented observation that is absent.')
        r['content'][0]['text']=json.dumps(a)
        got=self.call(raw=r)
        self.assertEqual(got['selected_urls'],['https://example.org/child'])
        self.assertEqual(got['ledger']['findings'],[])
    def test_bare_json_never_repairs_truncation_prose_or_duplicates(self):
        good=json.dumps(dict(findings=[],selected_urls=[]))
        for text,stop in [(good,'max_tokens'),(good[:-1],'end_turn'),('Here is the result: '+good,'end_turn'),
                          (good+good,'end_turn'),('{"findings":[],"findings":[],"selected_urls":[]}','end_turn')]:
            with self.subTest(text=text,stop=stop),self.assertRaises(Stop):
                self.call(raw=dict(stop_reason=stop,content=[dict(type='text',text=text)]))
    def test_whitespace_binding_has_original_offsets(self):
        b=json.loads(self.req['messages'][0]['content']);b['sources'][0]['spans'][0]['text']=OBS.replace('August ','August\n\t')
        p=copy.deepcopy(self.req);p['messages'][0]['content']=json.dumps(b)
        pos=self.call(request=p)['ledger']['findings'][0]['positions'][0]
        self.assertIn('\n\t',pos['original_quote']);self.assertEqual(pos['start'],0)

class PipelineTests(unittest.TestCase):
    def execute(self,mode='normal'):
        calls=[];saved=[];captures=[]
        def req(stage,p):
            calls.append((stage,copy.deepcopy(p)));b=json.loads(p['messages'][0]['content'])
            if stage=='search':return response('research',dict(question_id=Q['id'],needs=['Find the observed level.'],source_urls=['https://example.org/source']))
            if stage=='need':
                f=dict(need_id='n1',source_sha256=b['sources'][0]['original_sha256'],quote='A nonexistent but complete factual quotation.',kind='observation',series='test',unit='units',period='August',target_relevance='baseline',vintage_status='consistent')
                return response('research_quality',dict(findings=[f],selected_urls=[] if b['final'] else ['https://example.org/child']))
            if stage=='forecast':return response('forecast',dict(question_id=Q['id'],decision='forecast',distribution=dict(kind='binary',probability=.55),facts=[dict(claim=OBS,url='https://example.org/source',observation_date=None)],assumptions=['Subjective choice for the future.'],gaps=[],rationale='Future forecast.'),'max_tokens' if mode=='truncated_forecast' else 'end_turn')
            return response('compact_review',dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url='https://example.org/source',quote=OBS)])],unsupported=[],subjective=[]),'max_tokens' if mode=='truncated_review' else 'end_turn')
        def initial(q,urls,searched,dest):return dict(documents=[extract(urls[0],dict(type='text',data=OBS),TIME.isoformat())],failures=[])
        def follow(offered,selected,docs,dest):
            if mode=='global_source_stop':
                from operations_ledger_v1 import GlobalStop
                raise GlobalStop('Expired')
            if mode=='failed_child':
                captures.append(selected)
                return dict(documents=[],failures=[dict(error_type='HTTPError',http_status=403)])
            captures.append(selected);return dict(documents=[extract(selected[0],dict(type='text',data='Another source was read successfully.'),TIME.isoformat())],failures=[])
        with tempfile.TemporaryDirectory() as tmp:
            r=run_question(Q,tmp,request=req,count=lambda p:100,clock=lambda:TIME,save=saved.append,capture_initial=initial,capture_follow=follow,candidates=lambda *args:[dict(url='https://example.org/child')])
        return r,calls,captures,saved
    def test_pipeline_keeps_child_read_despite_bad_finding_and_uses_same_review_text(self):
        r,calls,captures,saved=self.execute()
        self.assertEqual(r['status'],'candidate');self.assertEqual(len(captures),1)
        self.assertEqual([s for s,p in calls],['search','need','forecast','review'])
        self.assertTrue(any(s['raw_answer'] and 'review' not in s for s in saved))
        self.assertEqual(json.loads(r['review_request']['messages'][0]['content'])['documents'],profile.audit_documents(r['forecast_request']))
        self.assertTrue(all(p['output_config']=={'effort':profile.EFFORT[stage]} for stage,p in calls))
        self.assertIn('Another source was read successfully.',r['forecast_request']['messages'][0]['content'])
        self.assertEqual(len(r['assessments']),1)
    def test_failed_child_uses_existing_sources_without_second_need(self):
        r,calls,captures,_=self.execute('failed_child')
        self.assertEqual(r['status'],'candidate')
        self.assertEqual([s for s,p in calls],['search','need','forecast','review'])
        self.assertEqual(len(r['documents']),1)
        self.assertEqual(r['child_capture']['failures'][0]['http_status'],403)
    def test_source_global_stop_prevents_forecast_and_review(self):
        r,calls,_,_=self.execute('global_source_stop')
        self.assertEqual(r['status'],'global_stop')
        self.assertEqual([s for s,p in calls],['search','need'])
    def test_review_cutoff_stays_error_without_another_call(self):
        r,calls,_,_=self.execute('truncated_review')
        self.assertEqual(r['status'],'candidate');self.assertIsNotNone(r['raw_answer'])
        self.assertEqual(r['review']['decision'],'unavailable')
        self.assertEqual(r['review']['labels'],['review_unavailable'])
        self.assertEqual(sum(stage=='review' for stage,p in calls),1)
    def test_zero_category_policy_does_not_modify_original(self):
        a=dict(decision='forecast',distribution=dict(kind='categorical',probabilities={'0':1,'1':0}))
        original=copy.deepcopy(a)
        with self.assertRaises(Stop):profile.check_distribution(a)
        self.assertEqual(a,original)
    def test_complete_json_from_truncated_forecast_never_accepted(self):
        r,calls,_,_=self.execute('truncated_forecast')
        self.assertEqual(r['status'],'validation_stop');self.assertIsNone(r['raw_answer'])
        self.assertFalse(any(s=='review' for s,p in calls))

if __name__=='__main__':unittest.main()
