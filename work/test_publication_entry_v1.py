import copy,datetime as dt,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from bounded_evidence import digest,extract
from candidate_api_v1 import MetaculusClient,ClaudeClient
from durable_candidate_host_v1 import database_bytes
from git_state_store_v1 import SnapshotStore
from operations_ledger_v1 import GlobalStop,Ledger
from publication_entry_v1 import run as entry_run,main,validate_launch as validate_entry,WORKFLOW,claim_launch
from test_git_state_store_v1 import FakeGit,REPO
from test_competition_adapter_v1 import NOW,post_fixture
from season_storage_simulation_v1 import OBS
import test_production_cycle_v1 as f


def run(*a,**kw):
    return entry_run(*a,source_commit='d'*40,workflow_path=WORKFLOW,**kw)

def validate_launch(*a,**kw):
    kw.setdefault('source_commit','d'*40);kw.setdefault('workflow_path',WORKFLOW)
    return validate_entry(*a,**kw)

def config(nonce='a'*32):
    t=f.CycleTests();t.setUp()
    p=dict(t.config,activation='publish_one_cycle',submit=True,actor_id=310015,state_branch='frogman-state-production-v1',resume_only_when_pending=True,pending_limit=1)
    p['launch']=dict(version='publication-launch-v2',config_sha256=digest(p),code_sha256='a'*64,
        source_commit='d'*40,workflow_path=WORKFLOW,nonce=nonce,
        expires_utc=p['job_deadline_utc'],approval_reference='synthetic-test-only')
    return p


class LaunchTests(unittest.TestCase):
    def test_disabled_has_no_inputs_or_side_effects(self):
        with self.assertRaisesRegex(GlobalStop,'disabled'):run(None,None,git=None,mc=None,claude=None,run_id=None,attempt=None,code_sha256=None,clock=None)
        with self.assertRaisesRegex(GlobalStop,'not activated'):main()
    def test_valid_frozen_launch(self):
        validate_launch(config(),run_id='123',attempt=1,code_sha256='a'*64,clock=lambda:NOW)
        # Same reviewed config can be prepared BEFORE GitHub creates the run.
        validate_launch(config(),run_id='987654321',attempt=1,code_sha256='a'*64,clock=lambda:NOW)
    def test_config_code_run_and_attempt_changes_rejected_before_io(self):
        for field,value in (('max_questions',2),('submit',False),('actor_id',500),('cap_usd','4'),('minimum_runtime_seconds',600)):
            p=config();p[field]=value
            with self.subTest(field=field),self.assertRaises(GlobalStop):
                run(p,None,git=None,mc=None,claude=None,run_id='123',attempt=1,code_sha256='a'*64,clock=lambda:NOW,enabled=True)
        for kwargs in (dict(run_id=''),dict(attempt=2),dict(code_sha256='b'*64),dict(source_commit='e'*40),dict(workflow_path='other.yml')):
            args=dict(run_id='123',attempt=1,code_sha256='a'*64,clock=lambda:NOW);args.update(kwargs)
            with self.assertRaises(GlobalStop):validate_launch(config(),**args)
    def test_expired_or_unbound_launch_cannot_start(self):
        for change in (dict(expires_utc=NOW.isoformat()),dict(approval_reference=''),dict(config_sha256='0'*64)):
            p=config();p['launch'].update(change)
            with self.assertRaises(GlobalStop):validate_launch(p,run_id='123',attempt=1,code_sha256='a'*64,clock=lambda:NOW)
        p=config();p['launch']['expires_utc']=(NOW+dt.timedelta(minutes=10)).isoformat()
        with self.assertRaises(GlobalStop):validate_launch(p,run_id='123',attempt=1,code_sha256='a'*64,clock=lambda:NOW)
    def test_resumption_finishes_before_any_new_listing(self):
        from test_publication_intent_v1 import PublicationTests
        t=PublicationTests();t.setUp()
        try:
            t.strand_comment_gap();t.t.config.update(resume_only_when_pending=True,pending_limit=1)
            def unexpected_listing(_):raise AssertionError('Resumption must not list new questions')
            r=t.run_cycle(listing=unexpected_listing)
            self.assertEqual(r['queue']['attempted'],[]);t.assert_complete()
        finally:t.doCleanups()
    def test_complete_shared_entry_chain_and_second_run_never_regenerates(self):
        class Git(FakeGit):
            def quota_observation(self):return dict(remaining=1000,observed_utc=NOW.isoformat(),reset_utc=(NOW+dt.timedelta(hours=1)).isoformat())
            def workflow_run(self,_):raise AssertionError('No recovery expected')
        p=config();git=Git();model=f.Model();server=f.Server();original=server.open
        def opened(req,timeout):
            if '/posts/?' in req.full_url:
                server.calls.append((req.get_method(),req.full_url));return f.Reply(dict(results=[post_fixture()],next=None))
            reply=original(req,timeout)
            if req.full_url.endswith('/questions/forecast/'):
                server.post['question']['my_forecasts']['latest']['author_id']=310015
            return reply
        server.open=opened
        def factory(host):
            def capture(q,urls,searched,folder):
                host.before_effect()
                return dict(documents=[extract(urls[0],dict(type='text',data=OBS),NOW.isoformat())],failures=[])
            return capture,lambda *a:dict(documents=[],failures=[])
        with tempfile.TemporaryDirectory() as tmp:
            s=SnapshotStore(git,repository=REPO,branch=p['state_branch'],period=p['period'],cap_units=3000000,code_sha256='a'*64,clock=lambda:NOW)
            l=Ledger(Path(tmp)/'initial',cap_usd='3',period_key=p['period'])
            try:s.bootstrap({'journal.sqlite3':database_bytes(l)})
            finally:l.close()
            with patch.object(f,'ME',dict(id=310015,username='Frogman-bot')),patch('production_entry_v1.source_factory',lambda:factory):
                for i in range(2):
                    pid=str(123+i);p=config(str(i+1)*32)
                    r=run(p,Path(tmp)/pid,git=git,mc=MetaculusClient('synthetic',opener=server),
                        claude=ClaudeClient('synthetic',opener=model),run_id=pid,attempt=1,code_sha256='a'*64,clock=lambda:NOW,enabled=True)
                    self.assertTrue(r['owner_released'])
                # Replaying even from a different run cannot take another question.
                before=list(server.calls);calls=model.model.calls
                with self.assertRaisesRegex(GlobalStop,'already consumed'):
                    run(p,Path(tmp)/'replayed',git=git,mc=MetaculusClient('synthetic',opener=server),
                        claude=ClaudeClient('synthetic',opener=model),run_id='999',attempt=1,
                        code_sha256='a'*64,clock=lambda:NOW,enabled=True)
                self.assertEqual(server.calls,before);self.assertEqual(model.model.calls,calls)
                self.assertEqual(model.model.calls,4)
                self.assertEqual(sum(m=='POST' for m,u in server.calls),2)
                self.assertEqual(len(server.comments),1)
                _,files=SnapshotStore(git,repository=REPO,branch=p['state_branch'],period=p['period'],cap_units=3000000,code_sha256='a'*64,clock=lambda:NOW).load();db=sqlite3.connect(':memory:')
                try:
                    db.deserialize(files['journal.sqlite3'])
                    self.assertEqual(db.execute('SELECT state FROM publications').fetchall(),[('complete',)])
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM publication_launches').fetchone(),(2,))
                finally:db.close()

    def test_nonce_and_old_run_bound_contract_rejected(self):
        for change in (dict(nonce='bad'),dict(nonce=None),dict(run_id='123'),dict(version='publication-launch-v1')):
            p=config();p['launch'].update(change)
            with self.assertRaises(GlobalStop):validate_launch(p,run_id='123',attempt=1,code_sha256='a'*64,clock=lambda:NOW)

    def test_live_cli_rejects_unpinned_workflow_before_clients(self):
        import json,os
        from production_entry_v1 import REPO
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json';p=config();path.write_text(json.dumps(p),encoding='utf-8')
            env=dict(FROGMAN_PUBLICATION_ENABLED='true',GITHUB_REPOSITORY=REPO,
                FROGMAN_INPUT=str(path),FROGMAN_INPUT_SHA=digest(p),GITHUB_SHA='d'*40,
                GITHUB_WORKFLOW_SHA='e'*40,GITHUB_WORKFLOW_REF=REPO+'/'+WORKFLOW+'@refs/heads/main')
            with patch.dict(os.environ,env,clear=True),self.assertRaisesRegex(GlobalStop,'Workflow source'):
                main()

    def test_nonce_checkpoint_failure_cannot_allow_external_effect(self):
        from test_git_state_store_v1 import bootstrap,store,OWNER,OTHER
        from durable_candidate_host_v1 import DurableHost
        for failure in ('before','after'):
            git=FakeGit();s=bootstrap(git)
            with tempfile.TemporaryDirectory() as tmp:
                h=DurableHost(s,OWNER,Path(tmp)/'host',clock=lambda:NOW,
                    credit_expires_utc='2026-10-21T00:00:00Z',key_expires_utc='2026-11-06T00:00:00Z')
                git.fail=failure
                try:
                    if failure=='before':
                        with patch('git_state_store_v1.time.sleep'),self.assertRaises(GlobalStop):claim_launch(h,config())
                        self.assertTrue(s.poisoned)
                    else:
                        claim_launch(h,config()) # exact readback proves the lost acknowledgement
                        _,files=store(git).load();db=sqlite3.connect(':memory:')
                        try:
                            db.deserialize(files['journal.sqlite3'])
                            self.assertEqual(db.execute('SELECT nonce FROM publication_launches').fetchall(),[('a'*32,)])
                        finally:db.close()
                        with self.assertRaisesRegex(GlobalStop,'already consumed'):claim_launch(h,config())
                finally:h.ledger.close();h.closed=True


if __name__=='__main__':unittest.main()
