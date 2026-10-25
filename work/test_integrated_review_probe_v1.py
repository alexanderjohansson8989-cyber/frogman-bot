import json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from integrated_review_probe_v1 import verify_completed,OLD_HEAD,OLD_RUN,BRANCH,PERIOD,REPO
from candidate_api_v1 import ClaudeClient
from fourtype_dryrun_v2 import run
from git_state_store_v1 import SnapshotStore
from question_archive_v1 import load_archived_session
from test_fourtype_dryrun_v2 import fixture
from test_fourtype_integration_v2 import synthetic_transport,initial
from test_git_state_store_v1 import FakeGit,NOW
from operations_ledger_v1 import GlobalStop
from advisor_policy import OPUS

class IntegratedReviewTests(unittest.TestCase):
    def test_previous_paid_cost_cannot_be_reset_or_unsettled(self):
        db=sqlite3.connect(':memory:');db.execute('CREATE TABLE operations(state TEXT,actual INTEGER)')
        db.execute("INSERT INTO operations VALUES('settled',2114560)")
        manifest={'owner':None,'sequence':165}
        fake=SimpleNamespace(head=OLD_HEAD,load=lambda:(manifest,{'journal.sqlite3':db.serialize()}))
        terminal=dict(id=int(OLD_RUN),run_attempt=1,status='completed',conclusion='success',repository={'full_name':REPO})
        git=SimpleNamespace(workflow_run=lambda r:terminal,head=lambda b:OLD_HEAD)
        inspection=dict(blocked=False,counts={'operations':20,'submissions':0,'sessions':4})
        with patch('integrated_review_probe_v1.SnapshotStore',return_value=fake),patch('integrated_review_probe_v1.inspect_database',return_value=inspection):
            self.assertEqual(verify_completed(git,lambda:NOW)['prior_actual_cost_units'],2114560)
            db.execute('UPDATE operations SET actual=0')
            with self.assertRaises(GlobalStop):verify_completed(git,lambda:NOW)
            db.execute("UPDATE operations SET actual=2114560,state='uncertain'")
            with self.assertRaises(GlobalStop):verify_completed(git,lambda:NOW)
        db.close()

    def test_four_types_classified_review_archive_and_no_duplicate_dispatch(self):
        p,mc=fixture();git=FakeGit();calls=[]
        class HTTP:
            def open(self,req,timeout):
                if req.get_method()=='GET':value={'id':OPUS}
                elif req.full_url.endswith('count_tokens'):value={'input_tokens':100}
                else:
                    payload=json.loads(req.data);calls.append(payload);value=synthetic_transport(payload)
                    if '<compact_review>' in payload['system']:
                        text=value['content'][0]['text'];body=json.loads(text.split('<compact_review>')[1].split('</compact_review>')[0])
                        body.pop('unsupported');body['issues']=[]
                        value['content'][0]['text']='<compact_review>'+json.dumps(body)+'</compact_review>'
                class Reply:
                    def __enter__(self):return self
                    def __exit__(self,*args):pass
                    def read(self,n):return json.dumps(value).encode()
                return Reply()
        client=ClaudeClient('synthetic',opener=HTTP())
        kwargs=dict(run_id='127',attempt=1,code_sha256='c'*64,clock=lambda:NOW,
            capture_initial=initial,capture_follow=lambda *a:dict(documents=[],failures=[]),source_candidates=lambda *a:[],require_rate=False,
            verify_previous=lambda *a:dict(prior_actual_cost_units=2114560),state_branch=BRANCH,state_period=PERIOD,
            prior_label='previous_paid_diagnostic',archive_finished=True)
        with tempfile.TemporaryDirectory() as tmp,patch('builtins.print'):
            result=run(p,git,mc,client,tmp+'/first',events=[],**kwargs)
            self.assertEqual(len(calls),16);self.assertTrue(result['owner_released'])
            self.assertTrue(all(r['status']=='prepared_local_only' for r in result['questions']))
            self.assertTrue(all(r['review_labels']==['review_pass'] for r in result['questions']))
            store=SnapshotStore(git,repository=REPO,branch=BRANCH,period=PERIOD,cap_units=9000000,code_sha256='c'*64,clock=lambda:NOW)
            _,files=store.load();db=sqlite3.connect(':memory:');db.deserialize(files['journal.sqlite3'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM model_dispatch_contracts').fetchone()[0],16)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM effect_permits').fetchone()[0],16)
            for key, in db.execute('SELECT key FROM sessions'):
                archived=load_archived_session(store,db,key)
                self.assertEqual(archived['result']['review']['labels'],['review_pass'])
            db.close()
            with self.assertRaises(GlobalStop):run(p,git,mc,client,tmp+'/second',events=[],**kwargs)
            self.assertEqual(len(calls),16)

if __name__=='__main__':unittest.main()
