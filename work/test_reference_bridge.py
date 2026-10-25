import copy
import json
import unittest
from bounded_evidence import extract, authorize_text_request, approved_payload
from independent_runner import Stop
from reference_bridge import prepare, propose, select
from rate_reference_candidate import reference


def response(answer, tag='forecast'):
    return {'content': [{'type': 'text', 'text': f'<{tag}>' + json.dumps(answer) + f'</{tag}>'}]}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.time = '2026-10-14T00:00:00+00:00'
        self.url = 'https://example.org/history'
        self.new_url = 'https://example.org/current'
        self.docs = [extract(self.url, {'type': 'text', 'data': 'Archived historical observations in a synthetic test.'}, self.time)]
        self.new = [extract(self.new_url, {'type': 'text', 'data': 'A newly observed measurable source fact.'}, self.time)]
        self.q = dict(id='synthetic', type='binary')
        self.baseline = dict(question_id='synthetic', decision='forecast', distribution=dict(kind='binary', probability=.3),
            facts=[dict(claim='Synthetic history fixture.', url=self.url, observation_date=None)],
            assumptions=['Fixture only.'], gaps=['Not a real forecast.'], rationale='Synthetic baseline for integration testing.')
        self.method = dict(id='fixture', artifact_sha256='a'*64, assumptions=['Synthetic.'], limitations=['Not calibrated.'])
        self.eligibility = dict(status='eligible', reason='Fixture test only.', review_id='fixture-review')

    def prepare(self, **kwargs):
        return prepare(self.q, self.baseline, self.docs, self.new, self.time,
            method=self.method, eligibility=self.eligibility, **kwargs)

    def proposal(self, prepared=None):
        prepared = prepared or self.prepare()
        candidate = copy.deepcopy(self.baseline)
        candidate['distribution']['probability'] = .5
        return propose(prepared, response(candidate), prepared['body']['request'])

    def audit(self, decision):
        return response(dict(decision=decision, reason='A synthetic audit decision.',
            quantitative_link='Subjective transfer for this integration fixture.',
            new_evidence=[dict(url=self.new_url, quote='newly observed measurable source fact', relevance='New observation.')],
            source_checks=[dict(url=self.new_url, target_match=True, temporal_consistency=True, reason='Fixture matches.')],
            assumptions=[], sensitivity=[dict(condition='Transfer fails', distribution=self.baseline['distribution'])]), 'update_review')

    def test_exact_preflight_reference_visible_and_copies(self):
        p = self.prepare()
        request = p['body']['request']
        body = json.loads(request['messages'][0]['content'])
        self.assertEqual(body['computed_reference']['answer'], self.baseline)
        seen = []
        approval = authorize_text_request(request, lambda x: seen.append(x) or {'input_tokens': 500}, 1)
        self.assertEqual(seen, [request])
        self.assertEqual(approved_payload(approval), request)
        self.baseline['distribution']['probability'] = .99
        self.assertEqual(p['body']['baseline']['distribution']['probability'], .3)

    def test_changed_candidate_requires_exact_audit(self):
        p = self.proposal()
        with self.assertRaises(Stop): select(p)
        wrong = copy.deepcopy(p['body']['audit_request']); wrong['system'] += ' changed'
        with self.assertRaises(Stop): select(p, self.audit('accept'), wrong)
        result = select(p, self.audit('accept'), p['body']['audit_request'])['body']
        self.assertEqual(result['selection'], 'candidate')
        self.assertTrue(result['semantic_review_pending'])
        self.assertFalse(result['future_accuracy_verified'])

    def test_rejection_never_validates_unreviewed_reference(self):
        for status, expected in [('eligible','reference'), ('unverified','abstain'), ('invalid','abstain')]:
            self.eligibility['status'] = status
            p = self.proposal()
            result = select(p, self.audit('reject'), p['body']['audit_request'])['body']
            self.assertEqual(result['selection'], expected)
            if expected == 'abstain': self.assertIsNone(result['selected_answer'])

    def test_unchanged_and_abstention_do_not_create_extra_audit(self):
        p = self.prepare()
        unchanged = propose(p, response(self.baseline), p['body']['request'])
        self.assertIsNone(unchanged['body']['audit_request'])
        self.assertEqual(select(unchanged)['body']['selection'], 'reference')
        self.eligibility['status'] = 'unverified'
        p = self.prepare()
        self.assertEqual(select(propose(p, response(self.baseline), p['body']['request']))['body']['selection'], 'abstain')
        abstain = {**self.baseline, 'decision': 'abstain', 'distribution': None}
        self.assertEqual(select(propose(p, response(abstain), p['body']['request']))['body']['selection'], 'abstain')

    def test_tampering_and_compaction_cannot_bypass_binding(self):
        p = self.prepare()
        changed = copy.deepcopy(p['body']['request']); changed['messages'][0]['content'] += ' '
        with self.assertRaises(Stop): propose(p, response(self.baseline), changed)
        p['body']['eligibility']['status'] = 'invalid'
        with self.assertRaises(Stop): propose(p, response(self.baseline), p['body']['request'])
        p = self.proposal(); p['body']['candidate']['distribution']['probability'] = .9
        with self.assertRaises(Stop): select(p, self.audit('accept'), p['body']['audit_request'])

    def test_repeated_evidence_cannot_accept_change(self):
        self.docs += self.new
        p = self.proposal()
        with self.assertRaisesRegex(Stop, 'Repeated'):
            select(p, self.audit('accept'), p['body']['audit_request'])

    def test_count_reference_to_deterministic_candidate_and_audit(self):
        self.q = dict(id='synthetic', type='discrete', choices=[str(i) for i in range(10)]+['10_plus'])
        ref = reference([0,1,1,0,0,0,0], window_seconds=86400, target_seconds=7*86400)
        self.baseline['distribution'] = dict(kind='categorical', probabilities=dict(zip(self.q['choices'], ref['parameter_uncertainty_reference']['probabilities'])))
        p = self.prepare(count_target=dict(kind='unbounded_nonnegative_integer', overflow_at=10))
        answer = {k:v for k,v in self.baseline.items() if k!='distribution'}
        answer['count_model'] = dict(components=[dict(label='synthetic adjusted', family='poisson', weight=1, mean=3)])
        candidate = propose(p, response(answer, 'count_proposal'), p['body']['request'])
        self.assertEqual(candidate['body']['computed_candidate']['calculation']['expected_count'], 3)
        self.assertAlmostEqual(sum(candidate['body']['candidate']['distribution']['probabilities'].values()), 1)
        result = select(candidate, self.audit('reject'), candidate['body']['audit_request'])['body']
        self.assertEqual(result['selected_answer'], self.baseline)

    def test_numeric_reference_uses_same_bridge(self):
        self.q = dict(id='synthetic', type='numeric', lower_bound=None, upper_bound=None)
        self.baseline['distribution'] = dict(kind='normal_mixture', lower_bound=None, upper_bound=None,
            components=[dict(weight=1, mean=100, sd=10)])
        p = self.prepare()
        candidate = copy.deepcopy(self.baseline); candidate['distribution']['components'][0]['mean'] = 105
        proposal = propose(p, response(candidate), p['body']['request'])
        self.assertEqual(select(proposal, self.audit('accept'), proposal['body']['audit_request'])['body']['selection'], 'candidate')


if __name__ == '__main__': unittest.main()
