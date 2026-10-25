import copy
import json
import unittest

from bounded_evidence import (extract, excerpt, forecast_payload,
                              authorize_text_request, approved_payload)
from independent_runner import Stop


class BoundedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.doc = extract('https://example.org/report',
                           {'type':'text','data':'Earlier observation. '*100})
        self.payload = forecast_payload({'id':'future','type':'binary'},
                                        [self.doc], '2026-10-13T00:00:00Z')

    def test_truncation_is_explicit_and_original_preserved(self):
        original = copy.deepcopy(self.doc)
        part = excerpt(self.doc, 100)
        self.assertTrue(part['truncated'])
        self.assertFalse(part['absence_claims_allowed'])
        self.assertEqual(part['sent_chars'], 100)
        self.assertEqual(part['spans'][0]['end'], 100)
        self.assertEqual(self.doc, original)

    def test_binary_and_tool_routes_cannot_pass_preflight(self):
        for key, value in [('tools', []), ('container', 'abc')]:
            p = copy.deepcopy(self.payload); p[key] = value
            with self.assertRaises(Stop):
                authorize_text_request(p, lambda _: self.fail('must not count'), 1)
        p = copy.deepcopy(self.payload)
        p['messages'][0]['content'] = [{'type':'document','source':{'type':'base64'}}]
        with self.assertRaises(Stop):
            authorize_text_request(p, lambda _: self.fail('must not count'), 1)

    def test_count_covers_full_input_and_hash_prevents_mutation(self):
        seen = []
        def count(p):
            seen.append(copy.deepcopy(p)); return {'input_tokens': 1000}
        approval = authorize_text_request(self.payload, count, 1)
        self.assertEqual(seen, [self.payload])
        self.assertEqual(approval['reserve_usd'], '0.1248')
        self.assertEqual(approved_payload(approval), self.payload)
        approval['payload']['messages'][0]['content'] += 'unaccounted'
        with self.assertRaises(Stop): approved_payload(approval)

    def test_input_and_budget_stop_before_generation(self):
        with self.assertRaises(Stop):
            authorize_text_request(self.payload, lambda _: {'input_tokens':30001}, 1)
        with self.assertRaises(Stop):
            authorize_text_request(self.payload, lambda _: {'input_tokens':1000}, .01)
        with self.assertRaises(Stop):
            authorize_text_request(self.payload, lambda _: {'input_tokens':True}, 1)

    def test_bad_pdf_and_unreadable_text_do_not_become_evidence(self):
        for source in [{'type':'base64','media_type':'application/pdf','data':'AAAA'},
                       {'type':'text','data':'  '},
                       {'type':'text','data':'a'*250001}]:
            with self.assertRaises(Stop): extract('https://example.org',source)

    def test_source_and_total_limits(self):
        with self.assertRaises(Stop): forecast_payload({},[self.doc]*6,'now')
        large = extract('https://example.org',{'type':'text','data':'x'*80000})
        different = [extract('https://example.org/'+str(i),
                             {'type':'text','data':str(i)*80000}) for i in range(3)]
        with self.assertRaises(Stop): forecast_payload({},different,'now',30000)

    def test_duplicate_content_preserves_provenance_without_duplicate_text(self):
        other = copy.deepcopy(self.doc); other['url']='https://example.org/copy'
        p = forecast_payload({},[self.doc,other],'now')
        sources=json.loads(p['messages'][0]['content'])['sources']
        self.assertEqual(len(sources),1)
        self.assertEqual([a['url'] for a in sources[0]['source_aliases']],
                         [self.doc['url'],other['url']])


if __name__ == '__main__': unittest.main()
