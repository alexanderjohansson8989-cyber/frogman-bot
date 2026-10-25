import unittest,json,tempfile,datetime as dt
from source_fallback_v1 import alternatives,capture_with_alternatives
from operations_ledger_v1 import GlobalStop

URL='https://chosen.example/a'
HITS=['https://chosen.example/b','https://second.example/a','https://second.example/b','https://third.example/a','https://fourth.example/a']
def reply(urls):return dict(content=[dict(type='web_search_tool_result',content=[dict(url=u) for u in urls])])
def empty(urls):return dict(documents=[],failures=[dict(url=u,error_type='HTTPError',http_status=403) for u in urls],source_attempts=len(urls))

class SourceFallbackTests(unittest.TestCase):
    def test_two_distinct_hosts_in_original_search_order_only(self):
        self.assertEqual(alternatives(reply(HITS),[URL],HITS),[HITS[1],HITS[3]])
        self.assertEqual(alternatives(reply(HITS),[URL],{HITS[3]}),[HITS[3]])
        self.assertEqual(alternatives(dict(content=[dict(type='text',text=HITS[1])]),[URL],HITS),[])
    def test_readable_initial_source_prevents_extra_reads(self):
        calls=[];first=dict(documents=[{'url':URL}],failures=[],source_attempts=1)
        def capture(q,urls,*a):calls.append(urls);return first
        result=capture_with_alternatives({},[URL],HITS,reply(HITS),'unused',capture=capture,guard=lambda:None)
        self.assertIs(result,first);self.assertEqual(calls,[[URL]])
    def test_failed_initial_then_bounded_alternatives_preserve_diagnostics(self):
        calls=[];first=empty([URL])
        def capture(q,urls,*a):
            calls.append(urls)
            return first if len(calls)==1 else dict(documents=[dict(url=urls[0])],failures=[dict(url=urls[1],error_type='HTTPError')],source_attempts=2)
        r=capture_with_alternatives({},[URL],HITS,reply(HITS),'unused',capture=capture,guard=lambda:None)
        self.assertEqual(calls,[[URL],[HITS[1],HITS[3]]]);self.assertEqual(r['source_attempts'],3)
        self.assertEqual(len(r['failures']),2);self.assertFalse(first['documents']);self.assertNotIn('fallback',first)
        self.assertEqual(r['fallback']['new_model_calls'],0)
    def test_all_failed_stops_after_single_alternative_batch(self):
        calls=[]
        def capture(q,urls,*a):calls.append(urls);return empty(urls)
        r=capture_with_alternatives({},[URL],HITS,reply(HITS),'unused',capture=capture,guard=lambda:None)
        self.assertEqual(len(calls),2);self.assertFalse(r['documents']);self.assertEqual(r['source_attempts'],3)
    def test_global_stop_is_not_converted_to_source_failure(self):
        calls=[]
        def capture(q,urls,*a):
            calls.append(urls)
            if len(calls)>1:raise GlobalStop('expired')
            return empty(urls)
        with self.assertRaises(GlobalStop):capture_with_alternatives({},[URL],HITS,reply(HITS),'unused',capture=capture,guard=lambda:None)
        self.assertEqual(len(calls),2)
    def test_guard_failure_prevents_any_source_effect(self):
        def guard():raise GlobalStop('ownership lost')
        def capture(*a):self.fail('source effect after failed guard')
        with self.assertRaises(GlobalStop):capture_with_alternatives({},[URL],HITS,reply(HITS),'unused',capture=capture,guard=guard)
    def test_pipeline_uses_recovered_text_without_another_search_or_generation(self):
        from candidate_pipeline_v1 import run_question
        from bounded_evidence import extract
        import test_candidate_pipeline_v1 as fixture
        source=HITS[1];calls=[];reads=[]
        def request(stage,p):
            calls.append(stage)
            if stage=='search':
                r=fixture.response('research',dict(question_id=fixture.Q['id'],needs=['Find observed context'],source_urls=[URL]))
                r['content']+=reply(HITS)['content'];return r
            if stage=='need':return fixture.response('research_quality',dict(findings=[],selected_urls=[]))
            if stage=='forecast':return fixture.response('forecast',dict(question_id=fixture.Q['id'],decision='forecast',distribution=dict(kind='binary',probability=.55),facts=[dict(claim=fixture.OBS,url=source,observation_date=None)],assumptions=['Subjective future estimate.'],gaps=[],rationale='Future forecast.'))
            return fixture.response('compact_review',dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url=source,quote=fixture.OBS)])],issues=[],subjective=[]))
        def capture(q,urls,*args):
            reads.append(urls)
            if urls==[URL]:return empty(urls)
            return dict(documents=[extract(source,dict(type='text',data=fixture.OBS))],failures=[dict(url=urls[1])],source_attempts=2)
        with tempfile.TemporaryDirectory() as tmp:
            r=run_question(fixture.Q,tmp,request=request,count=lambda p:100,clock=lambda:fixture.TIME,save=lambda r:None,
                capture_initial=capture,capture_follow=lambda *a:self.fail('No follow links selected'),candidates=lambda *a:[])
        self.assertEqual(r['status'],'candidate');self.assertEqual(calls,['search','need','forecast','review'])
        self.assertEqual(reads,[[URL],[HITS[1],HITS[3]]]);self.assertIn(source,r['forecast_request']['messages'][0]['content'])
        self.assertEqual(r['initial_capture']['fallback']['new_model_calls'],0)

if __name__=='__main__':unittest.main()
