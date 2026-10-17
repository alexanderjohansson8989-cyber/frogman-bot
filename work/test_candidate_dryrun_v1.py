import copy,json,tempfile,unittest
from pathlib import Path
from candidate_dryrun_v1 import run,validate_input
from candidate_api_v1 import ClaudeClient
from bounded_evidence import extract
from advisor_policy import OPUS
from operations_ledger_v1 import GlobalStop
from test_competition_adapter_v1 import bind_post,post_fixture,ME,NOW
from test_git_state_store_v1 import FakeGit

OBS='The synthetic historical baseline was 19 units.'
def input_fixture():return dict(version='candidate-dry-run-v1',publication=False,cap_usd='3',rates={OPUS:['4','20'],'search_request':'.01'},
    console_balance_usd='100',console_observed_utc=NOW.isoformat(),credit_expires_utc='2026-10-20T00:00:00Z',key_expires_utc='2026-11-06T00:00:00Z',
    binding=bind_post(post_fixture(),ME,tournament_id=33121,expected_username='Frogman-bot',now=NOW))

class MockClaude:
    def __init__(self):self.calls=[]
    def metadata(self):return dict(id=OPUS)
    def count(self,p):return 100
    def generate(self,p,permit):
        permit(p);self.calls.append(p);b=json.loads(p['messages'][0]['content'])
        if 'tools' in p:tag='research';a=dict(question_id=b['question']['id'],needs=['Find baseline'],source_urls=['https://example.org/source'])
        elif '<research_quality>' in p['system']:tag='research_quality';a=dict(findings=[],selected_urls=[])
        elif '<compact_review>' in p['system']:tag='compact_review';a=dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url='https://example.org/source',quote=OBS)])],unsupported=[],subjective=[])
        else:tag='forecast';a=dict(question_id=b['question']['id'],decision='forecast',distribution=dict(kind='binary',probability=.55),facts=[dict(claim=OBS,url='https://example.org/source',observation_date=None)],assumptions=['Subjective future assessment'],gaps=[],rationale='The synthetic baseline supports this subjective assessment with material uncertainty.')
        return dict(model=OPUS,stop_reason='end_turn',usage=dict(input_tokens=100,output_tokens=100),content=[dict(type='text',text='<'+tag+'>'+json.dumps(a)+'</'+tag+'>')])
class ReadOnlyMC:
    def fetch(self,pid):return ME,post_fixture()

class DryRunTests(unittest.TestCase):
    def test_missing_or_stale_shared_balance_and_expired_window_stop(self):
        for field,value in [('console_balance_usd',None),('console_balance_usd','2'),('console_observed_utc','2026-10-14T00:00:00Z'),('credit_expires_utc',NOW.isoformat())]:
            p=input_fixture();p[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(GlobalStop):validate_input(p,NOW)
    def test_complete_dry_run_has_zero_metaculus_writes_and_cannot_bootstrap_twice(self):
        git=FakeGit();claude=MockClaude()
        def capture(q,urls,searched,folder):
            folder.mkdir(parents=True,exist_ok=True);(folder/'toy.source').write_text(OBS,encoding='utf-8')
            return dict(documents=[extract(urls[0],dict(type='text',data=OBS),NOW.isoformat())],failures=[])
        with tempfile.TemporaryDirectory() as tmp:
            r=run(input_fixture(),git,ReadOnlyMC(),claude,Path(tmp)/'first',run_id='987',attempt=1,code_sha256='f'*64,clock=lambda:NOW,capture_initial=capture)
            self.assertEqual(r['status'],'prepared_local_only');self.assertEqual(len(claude.calls),4)
            self.assertEqual(r['forecast_posts'],r['comment_posts'],0);self.assertTrue(r['owner_released'])
            with self.assertRaises(GlobalStop):run(input_fixture(),git,ReadOnlyMC(),claude,Path(tmp)/'again',run_id='988',attempt=1,code_sha256='f'*64,clock=lambda:NOW,capture_initial=capture)
            self.assertEqual(len(claude.calls),4)
    def test_changed_live_question_stops_before_any_generation_or_state_write(self):
        class Changed(ReadOnlyMC):
            def fetch(self,pid):
                post=post_fixture();post['question']['fine_print']='Changed after freeze';return ME,post
        git=FakeGit();claude=MockClaude()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GlobalStop):run(input_fixture(),git,Changed(),claude,tmp,run_id='987',attempt=1,code_sha256='f'*64,clock=lambda:NOW)
        self.assertEqual(claude.calls,[]);self.assertEqual(git.refs,{})
    def test_workflow_attempt_two_cannot_start_even_with_new_folder(self):
        git=FakeGit();claude=MockClaude()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GlobalStop):run(input_fixture(),git,ReadOnlyMC(),claude,tmp,run_id='987',attempt=2,code_sha256='f'*64,clock=lambda:NOW)
        self.assertEqual(claude.calls,[]);self.assertEqual(git.refs,{})

if __name__=='__main__':unittest.main()
