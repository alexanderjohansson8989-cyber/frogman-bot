"""Synthetic regressions for the 26 October advisory findings. No network."""
import copy,json,tempfile,unittest,hashlib
import test_competition_adapter_v1 as af
import test_candidate_pipeline_v1 as pf
from bounded_evidence import digest,extract
from bounded_pipeline import audit_documents
from candidate_pipeline_v1 import run_question
from competition_adapter_v1 import prepare,check_plan
from context_excerpt_candidate import select_context_excerpt,_terms
from independent_runner import Stop
import candidate_profile_v1 as profile

def pipeline(probability):
    q=af.binding(af.post_fixture())['question'];calls=[]
    def request(stage,p):
        calls.append(stage)
        if stage=='search':return pf.response('research',dict(question_id=q['id'],needs=['Find observed level.'],source_urls=['https://example.org/source']))
        if stage=='need':return pf.response('research_quality',dict(findings=[],selected_urls=[]))
        if stage=='forecast':return pf.response('forecast',dict(question_id=q['id'],decision='forecast',distribution=dict(kind='binary',probability=probability),facts=[dict(claim=pf.OBS,url='https://example.org/source',observation_date=None)],assumptions=['Subjective future judgement.'],gaps=[],rationale='The future remains uncertain.'))
        return pf.response('compact_review',dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url='https://example.org/source',quote=pf.OBS)])],unsupported=[],subjective=[]))
    with tempfile.TemporaryDirectory() as tmp:
        result=run_question(q,tmp,request=request,count=lambda p:100,clock=lambda:pf.TIME,save=lambda r:None,
            capture_initial=lambda q,urls,searched,dest:dict(documents=[extract(urls[0],dict(type='text',data=pf.OBS),pf.TIME.isoformat())],failures=[]),
            capture_follow=lambda *a:None,candidates=lambda *a:[])
    return result,calls

class AdvisoryFixTests(unittest.TestCase):
    def test_invalid_binary_stops_before_review_without_mutating_probability(self):
        for probability in (0,0.0005,0.9995,1):
            with self.subTest(probability=probability):
                r,calls=pipeline(probability)
                self.assertEqual(r['status'],'validation_stop')
                self.assertEqual(calls,['search','need','forecast'])
                self.assertEqual(r['raw_answer']['distribution']['probability'],probability)
    def test_valid_boundaries_reach_review(self):
        for probability in (.001,.5,.999):
            r,calls=pipeline(probability)
            self.assertEqual(r['status'],'candidate');self.assertEqual(calls[-1],'review')
    def test_categorical_constraints_and_legacy_policy(self):
        q=af.binding(af.post_fixture('multiple_choice'))['question']
        for p in (.0005,.9995):
            a=dict(decision='forecast',distribution=dict(kind='categorical',probabilities={'Active A':p,'Active B':1-p}))
            before=copy.deepcopy(a)
            with self.assertRaises(Stop):profile.check_distribution(a,q)
            self.assertEqual(a,before)
        profile.check_distribution(dict(decision='forecast',distribution=dict(kind='categorical',probabilities={'A':.001,'B':.999})),q)
        # Generic historical questions have no platform floor; no implicit clipping.
        profile.check_distribution(dict(decision='forecast',distribution=dict(kind='binary',probability=0)))
    def test_long_sources_preserve_forecast_and_provenance_without_broken_links(self):
        b=af.binding(af.post_fixture());r=af.candidate(b,dict(kind='binary',probability=.55))
        urls=['https://example.org/data?series='+('a'*n) for n in (320,900,1700)]
        docs=[extract(u,dict(type='text',data=pf.OBS),pf.TIME.isoformat()) for u in urls]
        r['forecast_request']=profile.payload(b['question'],docs,pf.TIME.isoformat())
        r['raw_answer']['facts']=[dict(claim=pf.OBS,url=u,observation_date=None) for u in urls]
        r['review'].update(original_answer_sha256=digest(r['raw_answer']),usable_answer=copy.deepcopy(r['raw_answer']),documents_sha256=digest(audit_documents(r['forecast_request'])))
        before=copy.deepcopy(r);plan=prepare(b,r);check_plan(plan)
        self.assertEqual(r,before);self.assertEqual(plan['payload'][0]['probability_yes'],.55)
        self.assertLessEqual(len(plan['comment']['text']),1500)
        self.assertNotIn('https://example.org/data?',plan['comment']['text'])
        self.assertEqual(len(plan['comment_adaptation']['source_url_omissions']),3)
        self.assertEqual({x['url_sha256'] for x in plan['comment_adaptation']['source_url_omissions']},{hashlib.sha256(u.encode()).hexdigest() for u in urls})
    def test_short_comment_unchanged_and_sources_are_escaped(self):
        b=af.binding(af.post_fixture());r=af.candidate(b,dict(kind='binary',probability=.55))
        plan=prepare(b,r);self.assertIsNone(plan['comment_adaptation'])
        self.assertIn('https://example.org/source',plan['comment']['text'])
        u='https://example.org/source?x=1&y=2'
        doc=extract(u,dict(type='text',data=pf.OBS),pf.TIME.isoformat())
        r['forecast_request']=profile.payload(b['question'],[doc],pf.TIME.isoformat())
        r['raw_answer']['facts']=[dict(claim=pf.OBS,url=u,observation_date=None)]
        r['review'].update(original_answer_sha256=digest(r['raw_answer']),usable_answer=copy.deepcopy(r['raw_answer']),documents_sha256=digest(audit_documents(r['forecast_request'])))
        plan=prepare(b,r);check_plan(plan)
        self.assertIn('?x=1&amp;y=2',plan['comment']['text'])
    def test_criteria_only_series_is_selected_without_changing_source_bytes(self):
        generic='Future test information. '*110
        target='ZXQ98765Z selected series baseline. '+('Plain explanation. '*60)
        doc={'pages':[{'page':i+1,'text':generic} for i in range(8)]+[{'page':9,'text':target}]}
        q={'question':'Future test information?', 'criteria':'Use ZXQ98765Z selected series baseline.'}
        original=copy.deepcopy(doc);out=select_context_excerpt(doc,q,2400)
        self.assertIn('ZXQ98765Z',''.join(s['text'] for s in out['spans']))
        for s in out['spans']:self.assertEqual(s['text'],doc['pages'][s['page']-1]['text'][s['start']:s['end']])
        self.assertEqual(doc,original);self.assertLessEqual(out['sent_chars'],2400)
        self.assertFalse(out['absence_claims_allowed'])
    def test_criteria_url_markup_not_used_as_relevance_terms(self):
        q={'question':'Future level?', 'criteria':'<a href="https://tracker.invalid/noisetoken">ZXQ98765Z</a>',
           'fine_print':'Use VINTAGECODE.', 'description':'Background only BACKGROUNDONLY.'}
        terms=_terms(q,[])
        self.assertIn('zxq98765z',terms);self.assertIn('vintagecode',terms)
        self.assertFalse({'tracker','invalid','noisetoken','href','backgroundonly'} & terms)

if __name__=='__main__':unittest.main()
