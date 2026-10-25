import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import Mock
from question_source_links_v1 import criterion_links, research_payload, MAX_LINKS
from text_source_capture import capture_selected
from test_text_source_capture import Reply, public
from test_candidate_pipeline_v1 import Q, TIME, OBS, response
from candidate_pipeline_v1 import run_question
from independent_runner import Stop
from advisor_policy import OPUS
from operations_ledger_v1 import GlobalStop


class CriterionLinkTests(unittest.TestCase):
    def test_optional_null_fine_print_is_empty_not_a_pipeline_stop(self):
        self.assertEqual(criterion_links(dict(criteria='No source link', fine_print=None)), [])
        p = research_payload(dict(Q, fine_print=None), OPUS)
        self.assertEqual(json.loads(p['messages'][0]['content'])['question_source_links'], [])

    def test_literal_offsets_query_fragment_entities_and_duplicates(self):
        text = 'Use [series](https://example.org/a_(b)?x=1&amp;y=2#table).'
        q = dict(criteria=text, fine_print='https://example.org/a_(b)?x=1&y=2#other')
        links = criterion_links(q)
        self.assertEqual(len(links), 1)
        x = links[0]
        self.assertEqual(x['url'], 'https://example.org/a_(b)?x=1&y=2')
        self.assertEqual(text[x['start']:x['end']], x['literal'])
        self.assertEqual(x['field_sha256'], hashlib.sha256(text.encode()).hexdigest())

    def test_html_bare_and_escaped_markdown(self):
        q = dict(criteria='<a href="https://example.org/data">source</a> and '
                 r'[wiki](https://example.org/a_\(b\))', fine_print='See https://example.org/other.')
        self.assertEqual([x['url'] for x in criterion_links(q)],
                         ['https://example.org/data', 'https://example.org/a_(b)', 'https://example.org/other'])
        self.assertEqual(criterion_links(q)[-1]['field'], 'fine_print')

    def test_no_background_or_forged_metadata_or_relative_url(self):
        q = dict(description='https://example.org/background', criteria='/relative http://example.org',
                 question_source_links=[dict(url='https://example.org/forged')])
        self.assertEqual(criterion_links(q), [])
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(Stop):
            capture_selected(q, ['https://example.org/forged'], [], folder)

    def test_invalid_credentials_port_controls_and_bounded_candidates(self):
        q = dict(criteria='https://user:pw@example.org https://example.org:80 '
                 'https://example.org:bad https://example.org/&#10;bad')
        self.assertEqual(criterion_links(q), [])
        q = dict(criteria=' '.join('https://example.org/'+str(i) for i in range(100)))
        self.assertEqual(len(criterion_links(q)), MAX_LINKS)
        self.assertEqual(criterion_links(q)[-1]['url'], 'https://example.org/23')
        with self.assertRaises(Stop): criterion_links(dict(criteria='x'*200001))

    def test_prompt_and_capture_use_same_urls_without_mutating_question(self):
        q = dict(Q, criteria='Read https://example.org/series#observations')
        before = copy.deepcopy(q)
        p = research_payload(q, OPUS)
        body = json.loads(p['messages'][0]['content'])
        self.assertEqual(body['question_source_links'], criterion_links(q))
        self.assertEqual(q, before)
        self.assertEqual(p['tools'][0]['max_uses'], 2)
        with tempfile.TemporaryDirectory() as folder:
            r = capture_selected(q, [body['question_source_links'][0]['url']], [], folder,
                                 resolver=public, open_request=lambda req, **k: Reply(OBS.encode(), req.full_url, 'text/plain'))
        doc = r['documents'][0]
        self.assertEqual(doc['candidate_origin'], 'question_criteria_link')
        self.assertEqual(doc['question_link_provenance'], criterion_links(q)[0])
        self.assertFalse(doc['also_returned_by_search'])

    def test_guessed_path_rejected_and_three_source_limit_preserved(self):
        q = dict(criteria='https://example.org/real')
        transport = Mock()
        for urls in [['https://example.org/guess'], ['https://example.org/real']*4]:
            with self.subTest(urls=urls), tempfile.TemporaryDirectory() as folder, self.assertRaises(Stop):
                capture_selected(q, urls, [], folder, resolver=public, open_request=transport)
        transport.assert_not_called()

    def test_private_dns_still_rejected_before_http(self):
        transport = Mock()
        q = dict(criteria='https://example.org/data')
        with tempfile.TemporaryDirectory() as folder:
            r = capture_selected(q, ['https://example.org/data'], [], folder,
                                 resolver=lambda *a, **k: [(0,0,0,'',('127.0.0.1',443))], open_request=transport)
        transport.assert_not_called()
        self.assertEqual(r['documents'], [])
        self.assertEqual(len(r['failures']), 1)

    def test_global_stop_is_not_downgraded(self):
        q = dict(criteria='https://example.org/data')
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(GlobalStop):
            capture_selected(q, ['https://example.org/data'], [], folder, resolver=public,
                             open_request=Mock(side_effect=GlobalStop('Expired')))

    def test_pipeline_reads_criterion_without_search_hit_and_no_extra_model_call(self):
        url = 'https://example.org/source'
        q = dict(Q, criteria='Compare levels at '+url)
        calls = []; http = []
        def request(stage, p):
            calls.append(stage)
            if stage == 'search':
                self.assertEqual(json.loads(p['messages'][0]['content'])['question_source_links'][0]['url'], url)
                return response('research', dict(question_id=q['id'], needs=['Find baseline'], source_urls=[url]))
            if stage == 'need': return response('research_quality', dict(findings=[], selected_urls=[]))
            if stage == 'forecast':
                return response('forecast', dict(question_id=q['id'], decision='forecast',
                    distribution=dict(kind='binary', probability=.55), facts=[dict(claim=OBS, url=url, observation_date=None)],
                    assumptions=['Future uncertain'], gaps=[], rationale='Future forecast.'))
            return response('compact_review', dict(checks=[dict(fact_index=0, supported=True,
                evidence=[dict(url=url, quote=OBS)])], unsupported=[], subjective=[]))
        def open_request(req, **k):
            http.append(req.full_url)
            return Reply(OBS.encode(), req.full_url, 'text/plain')
        def initial(q, urls, searched, dest):
            return capture_selected(q, urls, searched, dest, resolver=public, open_request=open_request)
        with tempfile.TemporaryDirectory() as folder:
            r = run_question(q, folder, request=request, count=lambda p:100, clock=lambda:TIME,
                             save=lambda r:None, capture_initial=initial, capture_follow=Mock(), candidates=lambda *a:[])
        self.assertEqual(r['status'], 'candidate', r['diagnostics'])
        self.assertEqual(calls, ['search', 'need', 'forecast', 'review'])
        self.assertEqual(http, [url])
        self.assertEqual(r['documents'][0]['question_link_provenance'], criterion_links(q)[0])


if __name__ == '__main__': unittest.main()
