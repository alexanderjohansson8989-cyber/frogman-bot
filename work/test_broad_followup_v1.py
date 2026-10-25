"""Independent-audit follow-up: accounting contract and bounded review input."""
import copy,json,tempfile,unittest
from pathlib import Path
from advisor_policy import OPUS
from operations_ledger_v1 import Ledger,ReservedRequester,GlobalStop
from candidate_recovery_v1 import RecoveryHost,inspect
from test_git_state_store_v1 import FakeGit,bootstrap,store,OTHER
from test_candidate_runtime_v1 import new_host,terminal
from test_competition_adapter_v1 import NOW
from test_candidate_pipeline_v1 import Q,TIME,OBS,response
from candidate_pipeline_v1 import run_question
from bounded_evidence import extract
import candidate_profile_v1 as profile
from classified_review_v1 import eligible

def forecast_payload():
    return dict(model=OPUS,system='fixture',messages=[dict(role='user',content='fixture')],max_tokens=6000,output_config={'effort':'high'})
def usage(out):
    return dict(model=OPUS,usage=dict(input_tokens=1,output_tokens=out),stop_reason='end_turn',content=[])

class BroadFollowupTests(unittest.TestCase):
    def test_excess_output_is_charged_then_persistently_stopped_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            l=Ledger(Path(tmp)/'state',cap_usd='3',period_key='test');calls=[];raw=usage(7000)
            try:
                def transport(p):calls.append(p);return raw
                request=ReservedRequester(l,'q',transport=transport,count=lambda p:10000,guard=lambda:None)
                with self.assertRaisesRegex(GlobalStop,'Output exceeded'):request('forecast',forecast_payload())
                row=l.db.execute('SELECT state,actual,raw FROM operations').fetchone()
                self.assertEqual(row[:2],('settled',140004));self.assertEqual(json.loads(row[2]),raw)
                self.assertEqual(l.db.execute('SELECT stopped FROM policy').fetchone(),(1,))
                with self.assertRaises(GlobalStop):request('forecast',forecast_payload())
                self.assertEqual(len(calls),1)
            finally:l.close()

    def test_output_at_request_limit_remains_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            l=Ledger(Path(tmp)/'state',cap_usd='3',period_key='test')
            try:
                r=ReservedRequester(l,'q',transport=lambda p:usage(6000),count=lambda p:10000,guard=lambda:None)
                self.assertEqual(r('forecast',forecast_payload()),usage(6000))
                self.assertEqual(l.db.execute('SELECT stopped FROM policy').fetchone(),(0,))
            finally:l.close()

    def test_recovery_cannot_bypass_candidate_output_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            git=FakeGit();bootstrap(git);h=new_host(git,Path(tmp)/'old');p=forecast_payload()
            h.ledger.reserve('q/forecast/0','forecast',p,'.168');h.ledger.dispatch('q/forecast/0',p);h.ledger.save_raw('q/forecast/0',usage(7000))
            with self.assertRaises(GlobalStop):h.close()
            r=RecoveryHost(store(git),OTHER,Path(tmp)/'recovery',get_run=terminal,clock=lambda:NOW)
            try:
                with self.assertRaisesRegex(GlobalStop,'Recovered output exceeded'):r.settle_saved()
                self.assertEqual(r.ledger.db.execute('SELECT state,actual FROM operations').fetchone(),('settled',140004))
                self.assertEqual(r.ledger.db.execute('SELECT stopped FROM policy').fetchone(),(1,))
            finally:r.close()
            self.assertTrue(inspect(store(git))['blocked'])

    def test_review_alias_group_keeps_all_text_and_binds_each_url(self):
        source=OBS+' '+('Background material. '*200)
        docs=[extract('https://example.org/'+str(i),dict(type='text',data=source),TIME.isoformat()) for i in range(3)]
        f=profile.payload(Q,docs,TIME.isoformat());audit=profile.audit_documents(f)
        answer=dict(question_id=Q['id'],decision='forecast',distribution=dict(kind='binary',probability=.55),facts=[dict(claim=OBS,url=d['url'],observation_date=None) for d in docs],assumptions=[],gaps=[],rationale='Future forecast.')
        saved=copy.deepcopy(audit);p=profile.review_payload(Q,answer,audit);b=json.loads(p['messages'][0]['content'])
        self.assertEqual(audit,saved);self.assertEqual(len(b['documents']),1)
        group=b['documents'][0];urls=[group['url']]+[d['url'] for d in group['source_aliases']]
        self.assertEqual(set(urls),{d['url'] for d in audit});self.assertEqual(group['text'],audit[0]['text'])
        body=dict(checks=[dict(fact_index=i,supported=True,evidence=[dict(url=d['url'],quote=OBS)]) for i,d in enumerate(docs)],issues=[],subjective=[])
        checked=profile.review_check(response('compact_review',body),Q,answer,audit)
        self.assertEqual(checked['decision'],'pass');self.assertTrue(eligible(checked,Q,answer,audit))
        distinct=copy.deepcopy(audit);distinct[1]['text']+=' Different period.'
        self.assertEqual(len(json.loads(profile.review_payload(Q,answer,distinct)['messages'][0]['content'])['documents']),2)

    def run_pipeline(self,count):
        calls=[];doc=extract('https://example.org/source',dict(type='text',data=OBS),TIME.isoformat())
        def request(stage,p):
            calls.append(stage)
            if stage=='search':return response('research',dict(question_id=Q['id'],needs=['Find baseline.'],source_urls=[doc['url']]))
            if stage=='need':return response('research_quality',dict(findings=[],selected_urls=[]))
            if stage=='forecast':return response('forecast',dict(question_id=Q['id'],decision='forecast',distribution=dict(kind='binary',probability=.55),facts=[dict(claim=OBS,url=doc['url'],observation_date=None)],assumptions=['Future judgement.'],gaps=[],rationale='Future forecast.'))
            raise AssertionError('Oversized review must not be requested')
        with tempfile.TemporaryDirectory() as tmp:
            r=run_question(Q,tmp,request=request,count=count,clock=lambda:TIME,save=lambda _:None,capture_initial=lambda *a:dict(documents=[doc],failures=[]),capture_follow=lambda *a:dict(documents=[],failures=[]),candidates=lambda *a:[])
        return r,calls

    def test_oversized_review_keeps_raw_answer_with_explicit_unavailable_label(self):
        r,calls=self.run_pipeline(lambda p:31000 if '<compact_review>' in p['system'] else 100)
        self.assertEqual(calls,['search','need','forecast']);self.assertEqual(r['status'],'candidate')
        self.assertEqual(r['review_not_requested'],'input_limit');self.assertIsNone(r['review']['response'])
        self.assertEqual(r['review']['labels'],['review_unavailable']);self.assertEqual(r['raw_answer'],r['review']['original_answer'])
        self.assertTrue(eligible(r['review'],Q,r['raw_answer'],profile.audit_documents(r['forecast_request'])))

    def test_review_preflight_global_stop_is_not_downgraded(self):
        def count(p):
            if '<compact_review>' in p['system']:raise GlobalStop('Host expired')
            return 100
        r,calls=self.run_pipeline(count)
        self.assertEqual(r['status'],'global_stop');self.assertEqual(calls,['search','need','forecast'])
        self.assertNotIn('review',r)

if __name__=='__main__':unittest.main()
