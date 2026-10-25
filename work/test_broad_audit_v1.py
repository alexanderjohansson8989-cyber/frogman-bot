"""Offline regressions from the broad audit; no external service calls."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bounded_evidence import digest
from independent_runner import Stop
from research_quality import linked_candidates
from operations_ledger_v1 import GlobalStop
from candidate_recovery_v1 import RecoveryHost,inspect
from competition_adapter_v1 import submit_once
from question_archive_v1 import compact_completed
from test_git_state_store_v1 import FakeGit,bootstrap,store,OWNER,OTHER
from test_candidate_runtime_v1 import new_host,terminal,need
from test_competition_adapter_v1 import make_plan,post_fixture,ME,NOW

class BroadAuditTests(unittest.TestCase):
    def test_malformed_links_do_not_hide_valid_later_candidates(self):
        for bad in ('https://example.org:bad/x','https://example.org:70000/x','https://[broken/x'):
            raw=('<a href="'+bad+'">bad</a><a href="/good">good</a>').encode()
            doc=dict(url='https://example.org/source',network_media_type='text/html',network_original_sha256=digest(raw))
            with self.subTest(bad=bad):
                result=linked_candidates(doc,raw)
                self.assertEqual([x['url'] for x in result],['https://example.org/good'])
                self.assertEqual(result[0]['parent_source_sha256'],digest(raw))

    def test_original_identity_still_required(self):
        doc=dict(url='https://example.org/source',network_media_type='text/html',network_original_sha256='wrong')
        with self.assertRaisesRegex(Stop,'Original HTML identity'):linked_candidates(doc,b'<a href="/good">x</a>')

    def test_deadline_after_prepare_retains_recoverable_owner_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            git=FakeGit();bootstrap(git);h=new_host(git,Path(tmp)/'old');plan=make_plan();writes=[];clock=[NOW]
            original=h.ledger.prepare_submission
            def delayed(*args):
                original(*args);clock[0]=dt.datetime.fromisoformat(plan['binding']['cutoff_utc'])
            with patch.object(h.ledger,'prepare_submission',delayed):
                with self.assertRaisesRegex(Stop,'Submission deadline passed'):
                    submit_once(plan,h.ledger,fetch=lambda _: (ME,post_fixture()),post_forecast=writes.append,clock=lambda:clock[0],enabled=True)
            h.close();s=inspect(store(git))
            self.assertEqual(writes,[]);self.assertEqual(s['owner'],OWNER);self.assertTrue(s['blocked'])
            self.assertEqual([x['state'] for x in s['submissions']],['prepared'])
            with self.assertRaisesRegex(GlobalStop,'Interrupted state'):
                new_host(git,Path(tmp)/'forbidden')
            r=RecoveryHost(store(git),OTHER,Path(tmp)/'recovery',get_run=terminal,clock=lambda:NOW)
            r.abandon_finished_work();r.close();s=inspect(store(git))
            self.assertIsNone(s['owner']);self.assertFalse(s['blocked'])
            self.assertEqual([x['state'] for x in s['submissions']],['cancelled_unsent'])
            follow=new_host(git,Path(tmp)/'next')
            try:
                follow.ledger.start_session('next',{},'1');follow.ledger.finish_session('next',{'status':'done'})
                compact_completed(follow)
            finally:follow.close()
            self.assertEqual(writes,[])

    def test_prepared_model_without_session_retains_owner_until_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            git=FakeGit();bootstrap(git);h=new_host(git,Path(tmp)/'old')
            h.ledger.reserve('q/need/0','need',need(),'.120480');h.close()
            s=inspect(store(git));self.assertEqual(s['owner'],OWNER);self.assertTrue(s['blocked'])
            r=RecoveryHost(store(git),OTHER,Path(tmp)/'recovery',get_run=terminal,clock=lambda:NOW)
            r.abandon_finished_work();r.close()
            s=inspect(store(git));self.assertFalse(s['blocked']);self.assertIsNone(s['owner'])
            self.assertEqual([x['state'] for x in s['operations']],['cancelled_unsent'])

    def test_finished_host_still_releases_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            git=FakeGit();bootstrap(git);h=new_host(git,tmp);h.close()
            s=inspect(store(git));self.assertIsNone(s['owner']);self.assertFalse(s['blocked'])

    def test_git_failure_before_and_after_each_boundary_never_duplicates_dispatch(self):
        from audit_faultmatrix_20261020 import run
        result=run()
        self.assertEqual(result['case_count'],2*result['baseline']['git_calls'])
        self.assertGreaterEqual(result['case_count'],78)
        self.assertTrue(all(c['model_writes']<=1 for c in result['cases']))

    def test_cdf_matrix_preserves_input_and_meets_independent_mass_constraints(self):
        import math,random
        from cdf_projection_v1 import project_cdf
        rng=random.Random(20261021);cases=0
        for n in (1,2,17,199,200,201,400,2000):
            for low in (False,True):
                for high in (False,True):
                    for logarithmic in (False,True):
                        grid=[math.exp(10*i/n) if logarithmic else i/n for i in range(n+1)]
                        f=dict(inbound_outcome_count=n,open_lower_bound=low,open_upper_bound=high,scaling=dict(continuous_range=grid))
                        originals=[[0.]*(n+1),[1.]*(n+1),[0.]*(n//2+1)+[1.]*(n-n//2),
                                   sorted(rng.random()**8 for _ in range(n+1)),[i/n for i in range(n+1)]]
                        for original in originals:
                            saved=original[:];r=project_cdf(original,f);cdf=r['continuous_cdf']
                            self.assertEqual(original,saved);self.assertEqual(len(cdf),n+1)
                            self.assertTrue(all(math.isfinite(x) and 0<=x<=1 for x in cdf))
                            buckets=[round(b-a,9) for a,b in zip(cdf,cdf[1:])]
                            self.assertTrue(all(round(.01/n,9)<=x<=40/n for x in buckets))
                            self.assertTrue(cdf[0]>=.001 if low else cdf[0]==0)
                            self.assertTrue(cdf[-1]<=.999 if high else cdf[-1]==1)
                            self.assertEqual(project_cdf(cdf,f)['continuous_cdf'],cdf)
                            self.assertAlmostEqual(r['pmf_l1'],sum(abs(a-b) for a,b in zip(
                                [original[0]]+[b-a for a,b in zip(original,original[1:])]+[1-original[-1]],
                                [cdf[0]]+[b-a for a,b in zip(cdf,cdf[1:])]+[1-cdf[-1]])),places=12)
                            cases+=1
        self.assertEqual(cases,320)

if __name__=='__main__':unittest.main()
