"""Adversarial regressions from the independent review; no real network."""
import copy,datetime as dt,html,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from bounded_evidence import digest,extract
from independent_runner import Stop
from operations_ledger_v1 import Ledger,GlobalStop
from classified_review_v1 import evaluate,eligible
import test_classified_review_v1 as review_fixture
from test_classified_review_v1 import response
from test_competition_adapter_v1 import binding,post_fixture,make_plan,with_forecast,NOW,ME
from competition_adapter_v1 import verify_readback,submit_once,short_comment_rationale
from research_quality import linked_candidates,capture_children
from research_experiment_3 import candidates
from context_excerpt_candidate import _terms,_score
from candidate_api_v1 import JSONClient,MetaculusClient
from candidate_dispatch_v1 import forecast_transport
from test_candidate_runtime_v1 import new_host,FakeHTTP
from test_git_state_store_v1 import FakeGit,bootstrap

class ArgonRegressionTests(unittest.TestCase):
    def test_hard_survives_checks_issue_and_subjective_errors(self):
        f=review_fixture.ClassifiedReviewTests();f.setUp();hard=f.issue('target_mismatch',section='assumptions')
        bad=dict(f.issue(),evidence=[dict(url=f.docs[0]['url'],quote='short')])
        for issues in ([hard,bad],[bad,hard]):
            for break_field in ('checks','subjective','none'):
                b=copy.deepcopy(f.body);b['issues']=issues
                if break_field=='checks':b['checks'][0]['evidence'][0]['quote']='short'
                if break_field=='subjective':b['subjective']=None
                r=evaluate(response(b),f.q,f.a,f.docs)
                self.assertEqual(r['eligibility'],'hold')
                self.assertEqual(r,evaluate(response(b),f.q,f.a,f.docs))
                self.assertFalse(eligible(r,f.q,f.a,f.docs))
    def test_partial_rationale_binds_all_exact_occurrences(self):
        f=review_fixture.ClassifiedReviewTests();f.setUp()
        claim='A specific historical premise.'
        f.a['rationale']=claim+' More context. '+claim
        b=copy.deepcopy(f.body);b['issues']=[dict(f.issue('target_mismatch',section='rationale',index=None),claim=claim)]
        r=evaluate(response(b),f.q,f.a,f.docs)
        self.assertEqual(r['eligibility'],'hold')
        self.assertEqual(len(r['claim_bindings'][0]['positions']),2)
    def test_nullable_time_is_same_contract_and_readback(self):
        original=post_fixture();p=copy.deepcopy(original);p['question']['actual_close_time']=None
        self.assertEqual(binding(p)['question_contract_sha256'],binding(original)['question_contract_sha256'])
        plan=make_plan(p)
        self.assertTrue(verify_readback(plan,with_forecast(original,plan),ME)['verified'])
        self.assertTrue(verify_readback(plan,with_forecast(p,plan),ME)['verified'])
        p['question']['actual_close_time']='bad time'
        with self.assertRaises(Stop):binding(p)
    def test_null_scaling_has_controlled_stop(self):
        p=post_fixture('numeric');p['question']['scaling']=None
        with self.assertRaises(Stop):binding(p)
    def test_comment_escaped_budget_and_unchanged_original(self):
        for value in ('& '*300,'x'*640,'A complete sentence. '+'More & details. '*180):
            original=value
            text,adaptation=short_comment_rationale(value,escaped_limit=587)
            self.assertLessEqual(len(html.escape(text)),587)
            self.assertEqual(value,original)
            self.assertIsNotNone(adaptation)
        with self.assertRaises(Stop):short_comment_rationale('A sufficient explanation.',escaped_limit=20)
    def test_short_tokens_do_not_match_substrings(self):
        terms=_terms({'question':'GDP CPI AI UN EU UK oil gas Fed CO2 M2'},[])
        self.assertTrue({'gdp','cpi','ai','un','eu','uk','oil','gas','fed','co2','m2'}<=terms)
        self.assertEqual(_score('said main available under united until',{'ai','un'}),0)
        self.assertGreater(_score('AI and UN',{'ai','un'}),0)
    def test_small_legacy_limit_keeps_relevant_middle_and_exact_offsets(self):
        from bounded_evidence import select_excerpt
        text='ordinary prose. '*350+' GDP CPI oil gas observation. '+'ordinary prose. '*400
        d=extract('https://example.org/',dict(type='text',data=text))
        out=select_excerpt(d,{'question':'GDP CPI oil gas'},3000)
        self.assertIn('GDP CPI oil gas',' '.join(s['text'] for s in out['spans']))
        self.assertLessEqual(out['sent_chars'],3000)
        for s in out['spans']:self.assertEqual(text[s['start']:s['end']],s['text'])
    def test_invalid_hard_item_does_not_become_verified_rejection(self):
        f=review_fixture.ClassifiedReviewTests();f.setUp()
        b=copy.deepcopy(f.body);b['issues']=[dict(f.issue('target_mismatch'),claim='A different claim')]
        r=evaluate(response(b),f.q,f.a,f.docs)
        self.assertEqual(r['decision'],'unavailable')
        self.assertEqual(r['labels'],['review_unavailable'])
    def test_changed_actual_close_still_changes_contract(self):
        p=post_fixture();first=binding(p)
        p['question']['actual_close_time']='2026-10-15T23:00:00Z'
        self.assertNotEqual(first['question_contract_sha256'],binding(p)['question_contract_sha256'])
    def test_comment_pretransport_global_stop_does_not_read(self):
        from candidate_comments_v1 import submit
        from test_candidate_runtime_v1 import CommentClient
        git=FakeGit();bootstrap(git);plan=make_plan()
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(git,tmp);key='500/33121/102'
            h.ledger.prepare_submission(key,plan);h.ledger.begin_submission(key,plan)
            h.ledger.confirm_submission(key,plan,{'verified':True})
            client=CommentClient(plan);reads=[]
            def blocked(*args,**kw):raise GlobalStop('Pretransport stop')
            client.create_comment=blocked;client.comments=lambda *a:reads.append(a)
            try:
                with self.assertRaisesRegex(GlobalStop,'Pretransport'):submit(plan,h,client,enabled=True)
                self.assertEqual(reads,[])
            finally:
                with self.assertRaises(GlobalStop):h.close()
    def test_readback_rejects_poisoned_lost_owner_and_expired_window(self):
        for case in ('poisoned','owner','expiry'):
            git=FakeGit();bootstrap(git)
            with tempfile.TemporaryDirectory() as tmp:
                h=new_host(git,tmp)
                try:
                    if case=='poisoned':h.store.poisoned=True
                    elif case=='owner':h.store.manifest['owner']=None
                    deadline=NOW if case=='expiry' else NOW+dt.timedelta(seconds=90)
                    with self.assertRaises(GlobalStop):h.before_readback(deadline)
                finally:h.ledger.close()
    def test_permit_http_flag_stays_false_after_checkpoint_failure(self):
        from candidate_dispatch_v1 import DispatchPermit
        from candidate_api_v1 import TransportNotStarted
        git=FakeGit();bootstrap(git);plan=make_plan()
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(git,tmp);key='500/33121/102'
            h.ledger.prepare_submission(key,plan);h.ledger.begin_submission(key,plan)
            checks=[]
            def guard():
                checks.append(1)
                if len(checks)==2:raise GlobalStop('Failed second guard')
            permit=DispatchPermit(h.ledger,key,plan['payload'],kind='forecast',before_effect=guard)
            net=FakeHTTP()
            try:
                with self.assertRaises(TransportNotStarted):
                    MetaculusClient('synthetic',opener=net).forecast(plan['payload'],permit=permit)
                self.assertTrue(permit.used);self.assertFalse(permit.http_entered);self.assertEqual(net.calls,[])
            finally:
                with self.assertRaises(GlobalStop):h.close()
    def test_redirect_chain_is_excluded_in_all_layers(self):
        raw=b'<a href="/middle">Mid</a><a href="/final">Self</a><a href="/new">New</a>'
        d=dict(url='https://example.org/start',network_final_url='https://example.org/final',
               network_redirects=[dict(source='https://example.org/start',target='https://example.org/middle'),
                                  dict(source='https://example.org/middle',target='https://example.org/final')],
               network_media_type='text/html',network_original_sha256=digest(raw),archive_name='source.html')
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'source.html').write_bytes(raw)
            links=linked_candidates(d,raw)
            self.assertEqual([x['url'] for x in links],['https://example.org/new'])
            self.assertEqual(candidates({},[],[d],tmp),links)
            with patch('text_source_capture.capture') as fetch:
                with self.assertRaises(Stop):capture_children(links,[d['network_final_url']],[d],tmp)
                fetch.assert_not_called()
    def test_forecast_global_stop_before_http_never_reads_back(self):
        reads=[];plan=make_plan()
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'db',cap_usd='3',period_key='test')
            def fetch(pid):reads.append(pid);return ME,post_fixture()
            def blocked(p):raise GlobalStop('Owner lost before HTTP')
            try:
                with self.assertRaisesRegex(GlobalStop,'Owner lost'):
                    submit_once(plan,ledger,fetch=fetch,post_forecast=blocked,clock=lambda:NOW,enabled=True)
                self.assertEqual(len(reads),1)
                self.assertEqual(ledger.db.execute('SELECT state FROM submissions').fetchone()[0],'uncertain')
            finally:ledger.close()
    def test_http_boundary_distinguishes_local_serialization_and_timeout(self):
        from candidate_api_v1 import TransportNotStarted,TransportOutcomeUnknown
        net=FakeHTTP([TimeoutError()])
        client=JSONClient('https://example.org/',{},opener=net)
        with self.assertRaises(TransportNotStarted):client.call('test',method='POST',payload={'x':float('nan')},permit=lambda p:None)
        self.assertEqual(net.calls,[])
        with self.assertRaises(TransportOutcomeUnknown):client.call('test',method='POST',payload={},permit=lambda p:None)
        self.assertEqual(len(net.calls),1)
    def test_timeout_crossing_cutoff_can_confirm_but_no_resend(self):
        git=FakeGit();bootstrap(git);plan=make_plan()
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(git,tmp);clock=[NOW];h.clock=lambda:clock[0]
            class LateHTTP(FakeHTTP):
                def open(self,req,timeout):
                    clock[0]=dt.datetime.fromisoformat(plan['binding']['cutoff_utc'])+dt.timedelta(seconds=1)
                    return super().open(req,timeout)
            # Start close to cutoff so the independent 90s readback window holds.
            clock[0]=dt.datetime.fromisoformat(plan['binding']['cutoff_utc'])-dt.timedelta(seconds=1)
            net=LateHTTP([TimeoutError()]);send=forecast_transport(h,MetaculusClient('synthetic',opener=net),plan)
            reads=[]
            def fetch(pid):
                reads.append(pid)
                return ME,post_fixture() if len(reads)==1 else with_forecast(post_fixture(),plan)
            try:
                r=submit_once(plan,h.ledger,fetch=fetch,post_forecast=send,clock=h.clock,enabled=True)
                self.assertTrue(r['verified']);self.assertEqual(len(net.calls),1)
                self.assertEqual(h.ledger.db.execute('SELECT state FROM submissions').fetchone()[0],'verified')
            finally:h.close()

if __name__=='__main__':unittest.main()
