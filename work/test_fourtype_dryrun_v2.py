import copy,json,tempfile,unittest,datetime as dt
from types import SimpleNamespace
from unittest.mock import patch
from fourtype_dryrun_v2 import run,validate_input,ReadOnlyMetaculus
from competition_adapter_v1 import bind_test_post
from advisor_policy import OPUS
from test_fourtype_integration_v2 import synthetic_post,synthetic_transport,initial
from test_competition_adapter_v1 import ME,NOW
from test_git_state_store_v1 import FakeGit
from operations_ledger_v1 import GlobalStop
import sqlite3
from fourtype_zero_effects_v1 import verify_zero_effects,OLD_RUN,OLD_HEAD,REPO

def fixture():
    posts={};ids={};contracts={}
    for i,kind in enumerate(('binary','multiple_choice','numeric','discrete')):
        post=synthetic_post(kind);post['id']=101+i;post['question']['id']=201+i
        posts[post['id']]=post;ids[kind]=post['id']
        contracts[kind]=bind_test_post(post,ME,listing_post_ids=[post['id']],now=NOW)['question_contract_sha256']
    p=dict(version='fourtype-dryrun-v2',publication=False,cap_usd='9',rates={OPUS:['4','20'],'search_request':'.01'},
        posts=ids,contracts=contracts,console_balance_usd='100',console_observed_utc=NOW.isoformat(),
        credit_expires_utc='2026-10-20T00:00:00Z',key_expires_utc='2026-11-06T00:00:00Z')
    mc=SimpleNamespace(me=lambda:ME,post=lambda pid:copy.deepcopy(posts[pid]),http=SimpleNamespace(call=lambda p:dict(results=list(posts.values()))))
    return p,mc

class FourRunTests(unittest.TestCase):
    def test_replacement_requires_terminal_exact_state_and_no_effects(self):
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE checkpoint_events(sequence INTEGER,action TEXT)')
        for i,a in enumerate(['host-open','frozen-fourtype-input','session-start','pipeline-result']):db.execute('INSERT INTO checkpoint_events VALUES(?,?)',(i,a))
        fake=SimpleNamespace(head=OLD_HEAD,load=lambda:({'owner':{'run_id':OLD_RUN},'sequence':5},{'journal.sqlite3':db.serialize()}))
        terminal=dict(id=int(OLD_RUN),run_attempt=1,status='completed',conclusion='failure',repository={'full_name':REPO})
        g=SimpleNamespace(workflow_run=lambda r:terminal,head=lambda b:OLD_HEAD)
        inspection=dict(counts={'operations':0,'submissions':0,'sessions':1})
        with patch('fourtype_zero_effects_v1.SnapshotStore',return_value=fake),patch('fourtype_zero_effects_v1.inspect_database',return_value=inspection):
            self.assertEqual(verify_zero_effects(g,lambda:NOW)['actual_cost_units'],0)
            for field,value in [('status','in_progress'),('conclusion','cancelled')]:
                old=terminal[field];terminal[field]=value
                with self.assertRaises(GlobalStop):verify_zero_effects(g,lambda:NOW)
                terminal[field]=old
            for table in ('operations','submissions'):
                inspection['counts'][table]=1
                with self.assertRaises(GlobalStop):verify_zero_effects(g,lambda:NOW)
                inspection['counts'][table]=0
            fake.head='f'*40
            with self.assertRaises(GlobalStop):verify_zero_effects(g,lambda:NOW)
        db.close()
    def test_all_four_run_share_budget_and_single_use_state(self):
        p,mc=fixture();git=FakeGit();calls=[]
        def generate(payload,*,permit):permit(payload);calls.append(payload);return synthetic_transport(payload)
        claude=SimpleNamespace(metadata=lambda:dict(id=OPUS),count=lambda p:100,generate=generate)
        with tempfile.TemporaryDirectory() as tmp,patch('builtins.print'):
            result=run(p,git,mc,claude,tmp+'/first',run_id='123',attempt=1,code_sha256='d'*64,
                clock=lambda:NOW,events=[],capture_initial=initial,capture_follow=lambda *a:dict(documents=[],failures=[]),source_candidates=lambda *a:[],require_rate=False,verify_previous=lambda *a:dict(synthetic=True))
            self.assertEqual(len(result['questions']),4)
            self.assertTrue(all(q['status']=='prepared_local_only' for q in result['questions']))
            self.assertEqual(len(calls),16);self.assertTrue(result['owner_released']);self.assertEqual(result['uncertain_reserved_units'],0)
            self.assertLess(result['actual_cost_units'],9000000)
            with self.assertRaises(GlobalStop):
                run(p,git,mc,claude,tmp+'/second',run_id='124',attempt=1,code_sha256='d'*64,
                    clock=lambda:NOW,events=[],capture_initial=initial,capture_follow=lambda *a:dict(documents=[],failures=[]),source_candidates=lambda *a:[],require_rate=False,verify_previous=lambda *a:dict(synthetic=True))
            self.assertEqual(len(calls),16)
    def test_stale_balance_and_wrong_cap_stop_before_network(self):
        p,_=fixture()
        with self.assertRaises(GlobalStop):validate_input(p,NOW+dt.timedelta(hours=1))
        p['cap_usd']='10'
        with self.assertRaises(GlobalStop):validate_input(p,NOW)
    def test_metaculus_transport_forbids_writes_even_with_a_permit(self):
        class Denied:
            def open(self,*a,**kw):raise AssertionError('Network was touched')
        client=ReadOnlyMetaculus('synthetic',opener=Denied())
        for path in ('questions/forecast/','comments/create/','posts/123/'):
            with self.assertRaises(GlobalStop):client.http.call(path,method='POST',payload={},permit=lambda p:None)

if __name__=='__main__':unittest.main()
