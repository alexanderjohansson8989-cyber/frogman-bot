import sqlite3,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
from fourtype_parser_probe_v1 import verify_completed,OLD_RUN,OLD_HEAD,REPO,BRANCH,PERIOD
from fourtype_dryrun_v2 import run
from test_fourtype_dryrun_v2 import fixture
from test_fourtype_integration_v2 import synthetic_transport,initial
from test_competition_adapter_v1 import NOW
from test_git_state_store_v1 import FakeGit
from advisor_policy import OPUS
from operations_ledger_v1 import GlobalStop

class ParserRepeatTests(unittest.TestCase):
    def test_prior_cost_and_settlement_are_required(self):
        db=sqlite3.connect(':memory:');db.execute('CREATE TABLE operations(state TEXT,actual INTEGER)')
        db.execute("INSERT INTO operations VALUES('settled',1673480)")
        manifest={'owner':None,'sequence':129}
        fake=SimpleNamespace(head=OLD_HEAD,load=lambda:(manifest,{'journal.sqlite3':db.serialize()}))
        terminal=dict(id=int(OLD_RUN),run_attempt=1,status='completed',conclusion='success',repository={'full_name':REPO})
        git=SimpleNamespace(workflow_run=lambda r:terminal,head=lambda b:OLD_HEAD)
        inspection=dict(blocked=False,counts={'operations':16,'submissions':0,'sessions':4})
        with patch('fourtype_parser_probe_v1.SnapshotStore',return_value=fake),patch('fourtype_parser_probe_v1.inspect_database',return_value=inspection):
            self.assertEqual(verify_completed(git,lambda:NOW)['prior_actual_cost_units'],1673480)
            for state in ('uncertain','prepared'):
                db.execute('UPDATE operations SET state=?',(state,))
                with self.assertRaises(GlobalStop):verify_completed(git,lambda:NOW)
            db.execute("UPDATE operations SET state='settled',actual=0")
            with self.assertRaises(GlobalStop):verify_completed(git,lambda:NOW)
            db.execute('UPDATE operations SET actual=1673480')
            manifest['owner']={'run_id':OLD_RUN}
            with self.assertRaises(GlobalStop):verify_completed(git,lambda:NOW)
            manifest['owner']=None;terminal['status']='in_progress'
            with self.assertRaises(GlobalStop):verify_completed(git,lambda:NOW)
        db.close()

    def test_new_experiment_remains_single_use_and_retains_prior_cost(self):
        p,mc=fixture();git=FakeGit();calls=[]
        def generate(payload,*,permit):permit(payload);calls.append(payload);return synthetic_transport(payload)
        claude=SimpleNamespace(metadata=lambda:dict(id=OPUS),count=lambda p:100,generate=generate)
        kwargs=dict(run_id='125',attempt=1,code_sha256='e'*64,clock=lambda:NOW,
            capture_initial=initial,capture_follow=lambda *a:dict(documents=[],failures=[]),source_candidates=lambda *a:[],require_rate=False,
            verify_previous=lambda *a:dict(prior_actual_cost_units=1673480),state_branch=BRANCH,state_period=PERIOD,prior_label='previous_paid_diagnostic')
        with tempfile.TemporaryDirectory() as tmp,patch('builtins.print'):
            result=run(p,git,mc,claude,tmp+'/first',events=[],**kwargs)
            self.assertEqual(result['state_branch'],BRANCH)
            self.assertEqual(result['previous_paid_diagnostic']['prior_actual_cost_units'],1673480)
            self.assertTrue(result['owner_released']);self.assertEqual(len(calls),16)
            with self.assertRaises(GlobalStop):run(p,git,mc,claude,tmp+'/second',events=[],**kwargs)
            self.assertEqual(len(calls),16)

if __name__=='__main__':unittest.main()
