import copy,datetime as dt,json,tempfile,unittest
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from unittest.mock import patch
from production_adapters_v1 import list_posts,source_factory,PARAMS
from production_entry_v1 import run,main
from operations_ledger_v1 import GlobalStop,Ledger
from test_source_http_v1 import Response,resolver,URL
from test_competition_adapter_v1 import NOW,ME
from test_git_state_store_v1 import FakeGit,REPO
from durable_candidate_host_v1 import database_bytes
from git_state_store_v1 import SnapshotStore
import test_production_cycle_v1 as fixtures


class Feed:
    def __init__(self,pages):self.pages=iter(pages);self.http=self;self.calls=[]
    def call(self,path):self.calls.append(path);return next(self.pages)


def page(n=25,offset=0,next_offset=25):
    return dict(results=[dict(id=offset+i+1) for i in range(n)],
        next='https://www.metaculus.com/api/posts/?'+urlencode(dict(PARAMS,offset=str(next_offset))))


class ListingTests(unittest.TestCase):
    def test_short_page_ignores_infinite_next_and_keeps_four_types(self):
        f=Feed([page(2)]);guards=[]
        self.assertEqual(len(list_posts(f,33121,guard=lambda:guards.append(1))),2)
        self.assertEqual(len(guards),2);self.assertIn('discrete',f.calls[0]);self.assertEqual(len(f.calls),1)
    def test_two_pages_keep_filter_and_check_each_read(self):
        f=Feed([page(),page(3,25)]);guards=[]
        self.assertEqual(len(list_posts(f,33121,guard=lambda:guards.append(1))),28)
        self.assertEqual(len(guards),4);self.assertIn('offset=25',f.calls[1])
    def test_foreign_changed_or_duplicate_next_is_never_requested(self):
        for url in ('https://evil.example/api/posts/?x=y',
            'https://www.metaculus.com/api/posts/?'+urlencode(dict(PARAMS,offset='50')),
            'https://www.metaculus.com/api/posts/?'+urlencode(dict(PARAMS,offset='25'))+'&offset=25'):
            f=Feed([dict(page(),next=url)])
            with self.assertRaises(GlobalStop):list_posts(f,33121,guard=lambda:None)
            self.assertEqual(len(f.calls),1)
    def test_page_bound_and_duplicate_rows_refuse_partial_intake(self):
        for pages in ([page(),page(offset=25,next_offset=50),page(offset=50,next_offset=75)], [page(),page(1)]):
            with self.assertRaises(GlobalStop):list_posts(Feed(pages),33121,guard=lambda:None)
    def test_guard_failure_prevents_next_http(self):
        f=Feed([page(),page(1,25)]);calls=[0]
        def guard():
            calls[0]+=1
            if calls[0]==3:raise GlobalStop('expired')
        with self.assertRaises(GlobalStop):list_posts(f,33121,guard=guard)
        self.assertEqual(len(f.calls),1)


class Host:
    def __init__(self):self.before=0;self.local=0;self.expired=False
    def before_effect(self):
        self.before+=1
        if self.expired:raise GlobalStop('expired')
    def guard_local(self):
        self.local+=1
        if self.expired:raise GlobalStop('expired')


class CaptureTests(unittest.TestCase):
    def test_each_redirect_and_body_read_is_guarded(self):
        h=Host()
        class Opener:
            calls=0
            def open(self,req,**kw):
                self.calls+=1
                if self.calls==1:raise HTTPError(URL,302,'redirect',{'Location':'/new'},None)
                return Response(b'<p>Observed public value.</p>',req.full_url)
        o=Opener();initial,_=source_factory(opener=o,resolver=resolver)(h)
        with tempfile.TemporaryDirectory() as tmp:
            r=initial({},[URL],[URL],tmp)
        self.assertEqual(len(r['documents']),1);self.assertEqual(h.before,2);self.assertGreaterEqual(h.local,3)
    def test_expiry_during_read_propagates_global_and_no_second_source(self):
        h=Host()
        class LateResponse(Response):
            def read(self,n):h.expired=True;return super().read(n)
        class Opener:
            calls=0
            def open(self,req,**kw):self.calls+=1;return LateResponse(b'<p>Late.</p>',req.full_url)
        o=Opener();initial,_=source_factory(opener=o,resolver=resolver)(h)
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(GlobalStop):initial({},[URL,URL+'2'],[URL,URL+'2'],tmp)
        self.assertEqual(o.calls,1)
    def test_factory_is_connected_to_actual_cycle_host(self):
        t=fixtures.CycleTests();t.setUp();seen=[]
        def factory(host):
            seen.append(host)
            def capture(q,urls,searched,d):
                host.before_effect()
                from bounded_evidence import extract
                from season_storage_simulation_v1 import OBS
                return dict(documents=[extract(urls[0],dict(type='text',data=OBS),NOW.isoformat())],failures=[])
            return capture,lambda *a:dict(documents=[],failures=[])
        with tempfile.TemporaryDirectory() as tmp:r=t.run_cycle(tmp,capture_factory=factory)
        self.assertEqual(len(seen),1);self.assertTrue(seen[0].closed)
        self.assertEqual(r['queue']['attempted'][0]['status'],'prepared_local_only')


class EntryTests(unittest.TestCase):
    def test_disabled_precedes_all_config_credentials_and_io(self):
        with self.assertRaisesRegex(GlobalStop,'disabled'):run(None,None,git=None,mc=None,claude=None,run_id=None,attempt=None,code_sha256=None,clock=None)
        with patch.dict('os.environ',{},clear=True),self.assertRaisesRegex(GlobalStop,'disabled'):main()
    def test_submission_and_workflow_rerun_rejected_before_state_read(self):
        t=fixtures.CycleTests();t.setUp();p=dict(t.config,activation='prepare_only',state_branch='frogman-state-production-v1')
        for change in (dict(submit=True),dict(activation='publish'),dict(state_branch='other'),dict(state_branch='frogman-state-production-v1')):
            with self.assertRaises(GlobalStop):run(dict(p,**change),None,git=None,mc=None,claude=None,run_id='1',attempt=1,code_sha256='a'*64,clock=lambda:NOW,enabled=True)
        with self.assertRaises(GlobalStop):run(p,None,git=None,mc=None,claude=None,run_id='1',attempt=2,code_sha256='a'*64,clock=lambda:NOW,enabled=True)
    def test_existing_private_state_and_empty_intake_without_models(self):
        class Git(FakeGit):
            def quota_observation(self):return dict(remaining=1000,observed_utc=NOW.isoformat(),reset_utc=(NOW+dt.timedelta(hours=1)).isoformat())
            def workflow_run(self,_):raise AssertionError('No recovery needed')
        class Client(Feed):
            def me(self):return ME
        t=fixtures.CycleTests();t.setUp();p=dict(t.config,activation='prepare_only',state_branch='frogman-state-preparation-v1')
        git=Git()
        with tempfile.TemporaryDirectory() as tmp:
            s=SnapshotStore(git,repository=REPO,branch=p['state_branch'],period=p['period'],cap_units=3000000,code_sha256='a'*64,clock=lambda:NOW)
            l=Ledger(Path(tmp)/'initial',cap_usd='3',period_key=p['period'])
            try:s.bootstrap({'journal.sqlite3':database_bytes(l)})
            finally:l.close()
            r=run(p,Path(tmp)/'run',git=git,mc=Client([page(0)]),claude=None,run_id='123',attempt=1,code_sha256='a'*64,clock=lambda:NOW,enabled=True)
            self.assertEqual(r['queue']['attempted'],[]);self.assertTrue(r['owner_released'])


if __name__=='__main__':unittest.main()
