import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from git_state_store_v1 import SnapshotStore
from durable_candidate_host_v1 import DurableHost
from github_git_transport_v1 import GitHubGit,NoRedirect
from run_state_probe_v1 import run_probe
from operations_ledger_v1 import GlobalStop
from test_git_state_store_v1 import FakeGit,REPO,NOW,OWNER,store,bootstrap
from test_competition_adapter_v1 import bind_post,post_fixture,ME
from bounded_evidence import extract
from advisor_policy import OPUS

class ProofTests(unittest.TestCase):
    def test_two_fresh_jobs_restore_same_remote_bytes(self):
        git=FakeGit()
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
            first=run_probe('writer',git,a,run_id='987',attempt=1,code_sha256='e'*64,clock=lambda:NOW)
            second=run_probe('reader',git,b,run_id='987',attempt=1,code_sha256='e'*64,clock=lambda:NOW,expected_head=first['head'])
            self.assertTrue(second['owner_released']);self.assertGreater(second['sequence'],first['sequence'])
            self.assertEqual(second['claude_calls'],second['metaculus_calls'],0)
    def test_workflow_rerun_and_wrong_reader_head_do_not_write(self):
        git=FakeGit()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GlobalStop):run_probe('writer',git,tmp,run_id='987',attempt=2,code_sha256='e'*64,clock=lambda:NOW)
            self.assertEqual(git.refs,{})
            first=run_probe('writer',git,tmp,run_id='987',attempt=1,code_sha256='e'*64,clock=lambda:NOW)
            count=len(git.updated)
            with self.assertRaises(GlobalStop):run_probe('reader',git,Path(tmp)/'reader',run_id='987',attempt=1,code_sha256='e'*64,clock=lambda:NOW,expected_head='0'*40)
            self.assertEqual(len(git.updated),count)
    def test_fixed_git_origin_no_force_and_no_redirect(self):
        git=GitHubGit(REPO,'synthetic-test-secret')
        with self.assertRaises(GlobalStop):git._call('POST','/questions/123')
        with self.assertRaises(GlobalStop):git._call('GET','https://other.example/')
        with self.assertRaises(GlobalStop):git.update_ref('main','a'*40,force=False)
        with self.assertRaises(GlobalStop):git.update_ref('frogman-state-probe-test','a'*40,force=True)
        with self.assertRaises(GlobalStop):NoRedirect().redirect_request(None,None,302,None,None,None)
    def test_transport_failure_is_sanitized_without_retry(self):
        git=GitHubGit(REPO,'synthetic-test-secret')
        with patch.object(git.opener,'open',side_effect=TimeoutError('synthetic-test-secret')) as send:
            with self.assertRaises(GlobalStop) as err:git.repository()
            self.assertNotIn('synthetic-test-secret',str(err.exception));self.assertEqual(send.call_count,1)
    def test_question_deadline_during_ownership_read_stops_transport(self):
        git=FakeGit();bootstrap(git);now=[NOW];calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            host=DurableHost(store(git,clock=lambda:now[0]),OWNER,tmp,clock=lambda:now[0],
                credit_expires_utc='2026-10-21T00:00:00Z',key_expires_utc='2026-11-06T00:00:00Z')
            host.effect_deadline=NOW+dt.timedelta(seconds=1)
            original=git.head
            def slow_head(branch):
                result=original(branch);now[0]+=dt.timedelta(seconds=2);return result
            git.head=slow_head
            try:
                with self.assertRaises(GlobalStop):host.model_transport(calls.append)({'synthetic':True})
                self.assertEqual(calls,[])
            finally:git.head=original;host.close()
    def test_full_pipeline_uses_remote_journal_and_source_archive(self):
        git=FakeGit();bootstrap(git);obs='The synthetic historical baseline was 19 units.';calls=[]
        def transport(p):
            calls.append(p);body=json.loads(p['messages'][0]['content'])
            if 'tools' in p:tag='research';a=dict(question_id=body['question']['id'],needs=['Find baseline'],source_urls=['https://example.org/source'])
            elif '<research_quality>' in p['system']:tag='research_quality';a=dict(findings=[],selected_urls=[])
            elif '<compact_review>' in p['system']:tag='compact_review';a=dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url='https://example.org/source',quote=obs)])],unsupported=[],subjective=[])
            else:tag='forecast';a=dict(question_id=body['question']['id'],decision='forecast',distribution=dict(kind='binary',probability=.55),facts=[dict(claim=obs,url='https://example.org/source',observation_date=None)],assumptions=['Future judgement.'],gaps=[],rationale='A synthetic baseline supports this subjective future assessment with uncertainty.')
            return dict(model=OPUS,stop_reason='end_turn',usage=dict(input_tokens=100,output_tokens=100),content=[dict(type='text',text='<'+tag+'>'+json.dumps(a)+'</'+tag+'>')])
        def capture(q,urls,searched,folder):
            # Match the real text_source_capture module's original-file suffix.
            folder.mkdir(parents=True,exist_ok=True);(folder/'synthetic.source').write_text(obs,encoding='utf-8')
            return dict(documents=[extract(urls[0],dict(type='text',data=obs),NOW.isoformat())],failures=[])
        with tempfile.TemporaryDirectory() as tmp:
            host=DurableHost(store(git),OWNER,tmp,clock=lambda:NOW,credit_expires_utc='2026-10-21T00:00:00Z',key_expires_utc='2026-11-06T00:00:00Z')
            try:
                r=host.run(bind_post(post_fixture(),ME,tournament_id=33121,expected_username='Frogman-bot',now=NOW),
                    transport=transport,count=lambda p:100,capture_initial=capture,capture_follow=lambda *args:dict(documents=[],failures=[]),candidates=lambda *args:[])
                self.assertEqual(r['status'],'prepared_local_only');self.assertEqual(len(calls),4)
            finally:host.close()
        s=store(git);m,files=s.load();self.assertIsNone(m['owner'])
        self.assertTrue(any(n.endswith('.bin') and b==obs.encode() for n,b in files.items()))
        self.assertTrue(any(n.endswith('.json') and json.loads(b)['action']=='pipeline-result' for n,b in files.items()))

if __name__=='__main__':unittest.main()
