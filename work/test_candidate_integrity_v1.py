"""Targeted regressions from the pre-deployment integrity review; no network."""
import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from bounded_evidence import digest
from candidate_profile_v1 import OUT
from advisor_policy import OPUS
from independent_runner import Stop
from operations_ledger_v1 import Ledger, ReservedRequester, GlobalStop
from competition_adapter_v1 import prepare, verify_readback
from test_competition_adapter_v1 import binding, candidate, post_fixture, make_plan, with_forecast, ME, NOW


class IntegrityTests(unittest.TestCase):
    def test_review_must_match_exact_question(self):
        b = binding(post_fixture())
        r = candidate(b, dict(kind='binary', probability=.55))
        r['review']['question_sha256'] = digest(b['question'])
        self.assertEqual(prepare(b, r)['payload'][0]['question'], 102)
        for changed in (None, digest(dict(b['question'], criteria='Different resolution rule'))):
            bad = copy.deepcopy(r)
            if changed is None:
                del bad['review']['question_sha256']
            else:
                bad['review']['question_sha256'] = changed
            with self.subTest(changed=changed), self.assertRaises(Stop):
                prepare(b, bad)

    def test_dispatch_checkpoint_cannot_cross_deadline_then_call_transport(self):
        calls = []
        expired = [False]
        def guard():
            if expired[0]:
                raise GlobalStop('Deadline crossed during checkpoint')
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp)/'state.db', cap_usd='3', period_key='test')
            try:
                original_dispatch = ledger.dispatch
                def slow_dispatch(key, payload):
                    original_dispatch(key, payload)
                    expired[0] = True
                ledger.dispatch = slow_dispatch
                req = ReservedRequester(ledger, 'q', transport=calls.append, count=lambda p:100, guard=guard)
                p = dict(model=OPUS, system='Test', messages=[dict(role='user', content='Source')],
                         max_tokens=OUT['need'], output_config={'effort':'medium'})
                with self.assertRaises(GlobalStop):
                    req('need', p)
                self.assertEqual(calls, [])
                self.assertEqual(ledger.db.execute('SELECT state FROM operations').fetchone()[0], 'uncertain')
                with self.assertRaises(GlobalStop):
                    ledger.guard()
            finally:
                ledger.close()

    def test_readback_rejects_new_earlier_deadline(self):
        plan = make_plan()
        post = with_forecast(post_fixture(), plan)
        # Server now states that scoring closed BEFORE this returned forecast.
        post['question']['spot_scoring_time'] = (NOW+dt.timedelta(seconds=5)).isoformat()
        with self.assertRaises(Stop):
            verify_readback(plan, post, ME)

    def test_closed_post_can_confirm_on_time_forecast_with_unchanged_deadline(self):
        plan = make_plan()
        post = with_forecast(post_fixture(), plan)
        post['status'] = post['question']['status'] = 'closed'
        self.assertTrue(verify_readback(plan, post, ME)['verified'])


if __name__ == '__main__':
    unittest.main()
