import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bounded_evidence import extract, digest, forecast_payload
from general_evaluation import prepare_pair
from independent_runner import Stop
from research_quality import linked_candidates, capture_children, needs_record
from verified_statistics import count_summary, verify
from shared_token_fit import fit
from method_review import check, payload
import test_reference_bridge as fixtures


class StatisticsTests(unittest.TestCase):
    def test_actual_archived_frequencies(self):
        # Exact archived counts rather than a made-up successful AI answer.
        path=Path(__file__).resolve().parents[1]/'outputs/korningar/usgs_history_2/history.json'
        if not path.exists(): self.skipTest('Full history archive not in small CI bundle')
        raw=path.read_bytes(); h=json.loads(raw)
        r=count_summary(h['counts_including_zero'][-260:],selection=h['selection'],source_sha256=digest(raw))
        v=verify(r)['result']
        self.assertEqual((v['n'],v['total'],v['zero_windows'],v['at_least']),
                         (260,659,22,{'7':10,'10':2}))
        self.assertAlmostEqual(v['sample_variance'],3.747831897831898)

    def test_resealed_wrong_arithmetic_and_invalid_counts_rejected(self):
        r=count_summary([0,1,2,3],selection={'window':'fixture'},source_sha256='a'*64)
        r['body']['result']['total']=7; r['sha256']=digest(r['body'])
        with self.assertRaises(ValueError): verify(r)
        for counts in ([True,2], [1,-1], [1,2.5], [1]):
            with self.assertRaises(ValueError):count_summary(counts,selection={'x':1},source_sha256='a'*64)


class ResearchQualityTests(unittest.TestCase):
    def html(self):
        raw=b'<title>Index</title><a href="observed.json">data</a><a href="https://127.0.0.1/x">unsafe</a>'
        doc=extract('https://example.org/index/',dict(type='text',data='Index of directory'))
        doc.update(network_media_type='text/html',network_original_sha256=digest(raw),archive_name='index.source')
        return raw,doc

    def test_child_provenance_public_guard_remains_and_no_invented_link(self):
        raw,doc=self.html(); candidates=linked_candidates(doc,raw)
        self.assertEqual(candidates[0]['url'],'https://example.org/index/observed.json')
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'index.source').write_bytes(raw)
            with self.assertRaises(Stop):capture_children(candidates,['https://example.org/invented'],[doc],folder)
            with patch('text_source_capture.capture',side_effect=Stop('Non-public source address')) as cap:
                r=capture_children(candidates,['https://127.0.0.1/x'],[doc],folder)
                self.assertEqual(cap.call_count,1);self.assertEqual(r['source_attempts'],1)
                self.assertFalse(r['documents']);self.assertEqual(len(r['failures']),1)
            forged=copy.deepcopy(candidates);forged[0]['parent_source_sha256']='0'*64
            with self.assertRaises(Stop):capture_children(forged,[forged[0]['url']],[doc],folder)

    def test_index_is_not_an_observation_and_offsets_are_bound(self):
        text='Index of solar data directory, no actual measured values here.'
        d=extract('https://example.org/',dict(type='text',data=text))
        needs=[dict(id='recent',description='Recent observed monthly flux',required=True)]
        finding=dict(need_id='recent',source_sha256=d['original_sha256'],page_index=0,
            start=0,end=len(text),quote=text,kind='navigation',series='F10.7',unit='sfu',
            period='no observation period',target_relevance='Directory only',vintage_status='uncertain')
        r=needs_record({'id':'fixture'},needs,[d],[finding])
        self.assertEqual(r['body']['required_observation_needs_missing'],['recent'])
        finding['start']=1
        with self.assertRaises(Stop):needs_record({'id':'fixture'},needs,[d],[finding])


class SharedFitTests(unittest.TestCase):
    def test_both_branches_fit_before_freeze_and_selection_cannot_change(self):
        f=fixtures.BridgeTests();f.setUp()
        # Enough original text to exercise two deterministic selections.
        docs=[extract(f.url,dict(type='text',data='Measured historical weekly figures. '*500))]
        counts=iter([40000,42000,18000,19000]);seen=[]
        def build(ds):
            from general_evaluation import seal
            from reference_bridge import prepare
            b=prepare(f.q,f.baseline,ds,[],f.time,method=f.method,eligibility=f.eligibility)
            return seal(dict(request_A=forecast_payload(f.q,ds,f.time),prepared_B=b))
        def count(p):seen.append(copy.deepcopy(p));return {'input_tokens':next(counts)}
        result=fit(f.q,docs,build,count,limits=(12000,5000))
        self.assertEqual(len(seen),4);self.assertEqual(len(result['attempts']),2)
        self.assertNotIn('frozen_shared_excerpt',docs[0])
        changed=copy.deepcopy(result['documents'])
        changed[0]['frozen_shared_excerpt']['excerpt']['spans'][0]['text']='forged'
        with self.assertRaises(Stop):forecast_payload(f.q,changed,f.time)
        with self.assertRaises(Stop):forecast_payload({'id':'another'},result['documents'],f.time)

    def test_no_silent_raise_or_repair_when_no_shared_fit(self):
        q={'id':'fixture','type':'binary'}
        docs=[extract('https://example.org/',dict(type='text',data='original context '*1000))]
        with self.assertRaisesRegex(Stop,'still exceed'):
            fit(q,docs,lambda ds:prepare_pair(q,ds,'2026-10-20T01:00:00Z'),
                lambda p:{'input_tokens':30001},limits=(5000,3000))


class MethodTests(unittest.TestCase):
    def fixture(self):
        f=fixtures.BridgeTests();f.setUp();f.docs+=f.new
        p=f.proposal();pack=p['body']['pack']
        raw=f.audit('accept')
        a=json.loads(raw['content'][0]['text'].split('<update_review>')[1].split('</update_review>')[0])
        a.update(basis=['subjective_method'],computation_sha256=[])
        def reply(a):return fixtures.response(a,'method_review')
        return f,pack,a,reply

    def test_same_data_subjective_method_can_be_reviewed_without_new_citation(self):
        f,pack,a,reply=self.fixture()
        self.assertEqual(check(reply(a),pack)['selection'],'candidate')
        # Existing update rule still rejects repeated evidence.
        from update_review import check as old_check
        original={k:v for k,v in a.items() if k not in ('basis','computation_sha256')}
        with self.assertRaisesRegex(Stop,'Repeated'):old_check(fixtures.response(original,'update_review'),pack)
        a['basis']=['new_observation']
        with self.assertRaisesRegex(Stop,'labelled new'):check(reply(a),pack)

    def test_essential_unverified_facts_and_fake_computation_still_rejected(self):
        f,pack,a,reply=self.fixture()
        a['assumptions']=[dict(statement='invented variance',status='unverified_fact',essential=True)]
        with self.assertRaisesRegex(Stop,'Unsupported'):check(reply(a),pack)
        a['assumptions']=[];a['basis']=['verified_calculation'];a['computation_sha256']=['a'*64]
        with self.assertRaisesRegex(Stop,'Unverified computation'):check(reply(a),pack)

    def test_bridge_method_mode_preserves_legacy_default(self):
        f,pack,a,reply=self.fixture()
        from reference_bridge import prepare,propose,select
        p=prepare(f.q,f.baseline,f.docs,[],f.time,method=f.method,eligibility=f.eligibility,
                  audit_mode='same_data_method')
        candidate=copy.deepcopy(f.baseline);candidate['distribution']['probability']=.5
        proposed=propose(p,fixtures.response(candidate),p['body']['request'])
        self.assertIn('<method_review>',proposed['body']['audit_request']['system'])
        result=select(proposed,reply(a),proposed['body']['audit_request'])
        self.assertEqual(result['body']['selection'],'candidate')

    def test_recomputed_receipt_requires_source_binding(self):
        f,pack,a,reply=self.fixture()
        r=count_summary([0,1,2,3],selection={'window':'fixture'},source_sha256='a'*64)
        pack['verified_computations']=[r]
        with self.assertRaisesRegex(Stop,'not bound'):payload(pack)
        view=dict(history_sha256='a'*64,counts=[0,1,2,3],descriptive_statistics=r)
        pack['prior_documents'].append(dict(url='https://example.org/computation',text=json.dumps(view)))
        self.assertIn('verified_computations',payload(pack)['messages'][0]['content'])
        a['basis']=['verified_calculation'];a['computation_sha256']=[r['sha256']]
        self.assertEqual(check(reply(a),pack)['selection'],'candidate')


class EnrichmentAndFinalReviewTests(unittest.TestCase):
    def test_two_passes_then_no_more_reads_and_preserved_original(self):
        from research_enrichment import enrich
        raw=b'<a href="monthly.json">actual monthly data</a>'
        doc=extract('https://example.org/index/',dict(type='text',data='Index of monthly measured data'))
        doc.update(network_media_type='text/html',network_original_sha256=digest(raw),archive_name='index.source')
        measured='Observed monthly flux 120 sfu in September 2026.'
        child=extract('https://example.org/index/monthly.json',dict(type='text',data=measured))
        calls=[]
        def generate(p):
            calls.append(p)
            findings=[] if len(calls)==1 else [dict(need_id='flux',source_sha256=child['original_sha256'],
                quote=measured,kind='observation',series='F10.7',unit='sfu',period='September 2026',
                target_relevance='Recent predictor',vintage_status='consistent')]
            body=dict(findings=findings,selected_urls=[child['url']] if len(calls)==1 else [])
            return fixtures.response(body,'research_quality')
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,'index.source').write_bytes(raw)
            with patch('research_quality.capture_children',return_value=dict(documents=[child],failures=[],source_attempts=1)):
                # enrich imports the function directly; patch its own dependency.
                with patch('research_enrichment.capture_children',return_value=dict(documents=[child],failures=[],source_attempts=1)):
                    result=enrich({'id':'fixture','type':'binary'},
                        [dict(id='flux',description='Recent monthly flux',required=True)],
                        [doc],folder,generate=generate,research_time='2026-10-14T01:00:00Z')
        self.assertEqual(len(calls),2);self.assertTrue(result['can_proceed_to_forecast'])
        self.assertEqual(result['documents'][0],doc)
        self.assertTrue(result['semantic_review_pending'])

    def test_missing_observations_stop_even_with_readable_index(self):
        from research_enrichment import enrich
        doc=extract('https://example.org/',dict(type='text',data='Index of solar data files only'))
        calls=[]
        def generate(p):calls.append(p);return fixtures.response(dict(findings=[],selected_urls=[]),'research_quality')
        r=enrich({'id':'fixture','type':'binary'},[dict(id='flux',description='Recent observations',required=True)],
            [doc],'unused',generate=generate,research_time='2026-10-14T01:00:00Z')
        self.assertFalse(r['can_proceed_to_forecast']);self.assertEqual(len(calls),1)

    def test_final_review_is_blind_and_rejects_unsupported_pass_for_either_arm(self):
        from forecast_review import payload,check
        f=fixtures.BridgeTests();f.setUp()
        docs=[dict(url=f.url,text='Archived historical observations in a synthetic test.')]
        p=payload(f.q,f.baseline,docs)
        self.assertNotIn('arm',json.loads(p['messages'][0]['content']))
        a=dict(decision='pass',reason='Fixture factual support accepted.',
               fact_checks=[dict(fact_index=0,supported=True,url=f.url,quote=docs[0]['text'])],
               essential_unverified_premises=[],subjective_assumptions=['Transfer uncertainty.'])
        r=check(fixtures.response(a,'forecast_review'),f.q,f.baseline,docs)
        self.assertEqual(r['usable_answer'],f.baseline)
        a['essential_unverified_premises']=['Empirical variance invented from memory.']
        with self.assertRaisesRegex(Stop,'Unsupported pass'):
            check(fixtures.response(a,'forecast_review'),f.q,f.baseline,docs)
        a['decision']='reject'
        r=check(fixtures.response(a,'forecast_review'),f.q,f.baseline,docs)
        self.assertIsNone(r['usable_answer']);self.assertEqual(r['original_answer'],f.baseline)


if __name__=='__main__':unittest.main()
