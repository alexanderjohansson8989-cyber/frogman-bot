"""Fault-injected durable state tests. No HTTP or provider credentials."""
import copy
import datetime as dt
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from operations_ledger_v1 import Ledger,GlobalStop,ReservedRequester
from git_state_store_v1 import SnapshotStore,canonical,blob_sha,owner_id,sha256
from durable_candidate_host_v1 import DurableHost,database_bytes,inspect_database
from test_competition_adapter_v1 import NOW,ME,post_fixture,bind_post,make_plan,with_forecast
from competition_adapter_v1 import submit_once
from candidate_profile_v1 import OUT
from checkpoint_objects_v1 import events_from_snapshot
from advisor_policy import OPUS

REPO='alexanderjohansson8989-cyber/metaculus-bot'
BRANCH='frogman-state-probe-v1'
OWNER=owner_id('123',1,'a'*32)
OTHER=owner_id('124',1,'b'*32)

class FakeGit:
    def __init__(self):
        self.refs={};self.blobs={};self.trees={};self.commits={};self.private=True
        self.fail=None;self.updated=[];self.before_update=None
    def repository(self):return dict(full_name=REPO,private=self.private)
    def head(self,b):return self.refs.get(b)
    def commit(self,s):return copy.deepcopy(self.commits[s])
    def tree(self,s):return copy.deepcopy(self.trees[s])
    def blob(self,s):return self.blobs[s]
    def create_blob(self,data):
        if self.fail=='blob':raise TimeoutError()
        s=blob_sha(data);self.blobs[s]=data;return s
    def create_tree(self,entries):
        s=hashlib.sha1(canonical(entries)).hexdigest();self.trees[s]=copy.deepcopy(entries);return s
    def create_commit(self,tree,parents):
        s=hashlib.sha1(canonical(dict(tree=tree,parents=parents))).hexdigest()
        self.commits[s]=dict(sha=s,tree=dict(sha=tree),parents=[dict(sha=x) for x in parents]);return s
    def create_ref(self,b,s):
        if b in self.refs:raise ValueError('exists')
        self.refs[b]=s
    def update_ref(self,b,s,*,force=False):
        assert force is False
        if self.before_update:
            callback=self.before_update;self.before_update=None;callback()
        if self.fail=='before':raise TimeoutError()
        if self.commits[s]['parents'] != [dict(sha=self.refs[b])]:raise ValueError('not fast forward')
        self.refs[b]=s;self.updated.append(s)
        if self.fail=='after':raise TimeoutError()

def store(git,clock=lambda:NOW,period='synthetic-test'):
    return SnapshotStore(git,repository=REPO,branch=BRANCH,period=period,cap_units=3000000,code_sha256='c'*64,clock=clock)

def bootstrap(git):
    s=store(git)
    with tempfile.TemporaryDirectory() as tmp:
        ledger=Ledger(Path(tmp)/'bootstrap.db',cap_usd='3',period_key='synthetic-test')
        try:s.bootstrap({'journal.sqlite3':database_bytes(ledger)})
        finally:ledger.close()
    return s

class StateTests(unittest.TestCase):
    def setUp(self):
        self.git=FakeGit();self.store=bootstrap(self.git)
    def host(self,folder,owner=OWNER,clock=lambda:NOW,expires='2026-10-21T00:00:00Z'):
        return DurableHost(store(self.git,clock),owner,folder,clock=clock,credit_expires_utc=expires,key_expires_utc='2026-11-06T00:00:00Z')
    def test_missing_state_never_bootstraps_and_bootstrap_never_overwrites(self):
        g=FakeGit()
        with self.assertRaises(GlobalStop):store(g).load()
        self.assertEqual(g.refs,{})
        old=self.store.head
        with self.assertRaises(GlobalStop):self.store.bootstrap(self.store.files)
        self.assertEqual(self.git.head(BRANCH),old)
    def test_two_claimants_from_same_parent_have_one_winner(self):
        a=store(self.git);a.load();b=store(self.git);b.load()
        # B wins after A checks H, before A updates its sibling commit.
        self.git.before_update=lambda:b.claim(OTHER)
        with self.assertRaises(GlobalStop):a.claim(OWNER)
        self.assertEqual(store(self.git).load()[0]['owner'],OTHER)
        self.assertTrue(a.poisoned)
    def test_lost_successful_ack_is_confirmed_by_exact_readback(self):
        self.git.fail='after';self.store.claim(OWNER)
        self.assertEqual(self.store.manifest['owner'],OWNER)
        self.assertFalse(self.store.poisoned)
    def test_delayed_readback_retries_only_reads(self):
        old=self.store.head;original=self.git.head;update=self.git.update_ref;stale=[]
        def write(*args,**kwargs):
            update(*args,**kwargs);stale.extend([old,old])
        self.git.update_ref=write
        self.git.head=lambda b:stale.pop(0) if stale else original(b)
        with patch('git_state_store_v1.time.sleep') as sleep:self.store.claim(OWNER)
        self.assertEqual(len(self.git.updated),1);self.assertEqual(sleep.call_count,2)
        self.assertEqual(self.store.head,original(BRANCH))
    def test_persistent_old_readback_stops_after_four_gets(self):
        old=self.store.head;original=self.git.head;update=self.git.update_ref;reads=[]
        def write(*args,**kwargs):
            update(*args,**kwargs)
            def stale(b):reads.append(b);return old
            self.git.head=stale
        self.git.update_ref=write
        with patch('git_state_store_v1.time.sleep'),self.assertRaises(GlobalStop):self.store.claim(OWNER)
        self.assertEqual(len(reads),4);self.assertEqual(len(self.git.updated),1)
        self.assertTrue(self.store.poisoned);self.assertNotEqual(original(BRANCH),old)
    def test_lost_unsuccessful_ack_stops_without_rebasing_or_stealing(self):
        old=self.store.head;self.git.fail='before'
        with self.assertRaises(GlobalStop):self.store.claim(OWNER)
        self.assertEqual(self.git.head(BRANCH),old);self.assertTrue(self.store.poisoned)
        with self.assertRaises(GlobalStop):self.store.claim(OWNER)
    def test_public_repo_and_changed_budget_or_code_are_rejected(self):
        self.git.private=False
        with self.assertRaises(GlobalStop):store(self.git).load()
        self.git.private=True
        with self.assertRaises(GlobalStop):store(self.git,period='new-month').load()
        s=store(self.git);s.code='d'*64
        with self.assertRaises(GlobalStop):s.load()
    def test_tampered_blob_and_missing_database_are_rejected(self):
        h=blob_sha(self.store.files['journal.sqlite3']);old=self.git.blobs[h]
        self.git.blobs[h]=b'bad'
        with self.assertRaises(GlobalStop):store(self.git).load()
        self.git.blobs[h]=old
        with self.assertRaises(GlobalStop):self.store.checkpoint({},OWNER)
        with self.assertRaises(GlobalStop):inspect_database(b'not a sqlite database',self.store)
    def test_interrupted_owner_is_not_stolen_even_without_uncertain_operation(self):
        self.store.claim(OWNER)
        with self.assertRaises(GlobalStop):self.store.claim(OTHER)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GlobalStop):self.host(tmp,OTHER)
    def test_complete_session_restores_in_another_local_directory(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
            host=self.host(a);host.ledger.start_session('q',{},'1')
            host.ledger.finish_session('q',{'status':'done'});host.close()
            next_host=self.host(b,OTHER)
            try:
                self.assertEqual(next_host.ledger.db.execute('SELECT key,status FROM sessions').fetchall(),[('q','complete')])
                with self.assertRaises(GlobalStop):next_host.ledger.start_session('q',{},'1')
            finally:next_host.close()
    def request(self,host,transport):
        req=ReservedRequester(host.ledger,'q',transport=host.model_transport(transport),count=lambda p:100,guard=host.before_effect)
        p=dict(model=OPUS,system='Synthetic',messages=[dict(role='user',content='Synthetic source text')],max_tokens=OUT['need'],output_config={'effort':'medium'})
        return req,p
    def test_uncertain_dispatch_and_full_request_exist_before_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=self.host(tmp);calls=[]
            def transport(payload):
                m,files=store(self.git).load();inspection=inspect_database(files['journal.sqlite3'],self.store)
                self.assertTrue(inspection['blocked'])
                archives=events_from_snapshot(files)
                request=[x for x in archives if x['action']=='operation-request'][0]
                self.assertEqual(request['arguments'][2],payload)
                self.assertEqual(m['owner'],OWNER);calls.append(payload);raise TimeoutError()
            req,p=self.request(host,transport)
            try:
                with self.assertRaises(GlobalStop):req('need',p)
                with self.assertRaises(GlobalStop):req('need',p)
                self.assertEqual(len(calls),1)
            finally:
                with self.assertRaises(GlobalStop):host.close()
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(GlobalStop):self.host(other,OTHER)
    def test_failed_raw_checkpoint_leaves_remote_uncertain_and_no_next_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=self.host(tmp);calls=[]
            def transport(payload):
                calls.append(payload);self.git.fail='blob'
                return dict(model=OPUS,usage=dict(input_tokens=100,output_tokens=100),stop_reason='end_turn',content=[])
            req,p=self.request(host,transport)
            try:
                with self.assertRaises(GlobalStop):req('need',p)
                self.git.fail=None
                with self.assertRaises(GlobalStop):req('need',p)
                fresh=store(self.git);_,files=fresh.load()
                self.assertTrue(inspect_database(files['journal.sqlite3'],fresh)['blocked'])
                self.assertEqual(len(calls),1)
            finally:
                with self.assertRaises(GlobalStop):host.close()
    def test_expiry_during_dispatch_checkpoint_prevents_model_call(self):
        current=[NOW];expiry=(NOW+dt.timedelta(seconds=1)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            host=self.host(tmp,clock=lambda:current[0],expires=expiry)
            original=self.git.update_ref;calls=[]
            def update(b,s,**kwargs):
                original(b,s,**kwargs)
                commit=self.git.commits[s];tree=self.git.trees[commit['tree']['sha']]
                manifest=json.loads(self.git.blobs[next(e['sha'] for e in tree if e['path']=='manifest.json')])
                db=__import__('sqlite3').connect(':memory:')
                try:
                    db.deserialize(self.git.blobs[manifest['files']['journal.sqlite3']['git_blob']])
                    if db.execute("SELECT COUNT(*) FROM operations WHERE state='uncertain'").fetchone()[0]:current[0]+=dt.timedelta(seconds=2)
                finally:db.close()
            self.git.update_ref=update;req,p=self.request(host,calls.append)
            try:
                with self.assertRaises(GlobalStop):req('need',p)
                self.assertEqual(calls,[])
            finally:
                with self.assertRaises(GlobalStop):host.close()
    def test_expired_credit_period_stops_before_claim_or_restore(self):
        old=self.git.head(BRANCH)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GlobalStop):self.host(tmp,expires=NOW.isoformat())
            self.assertFalse((Path(tmp)/'journal.sqlite3').exists())
        self.assertEqual(self.git.head(BRANCH),old)
    def test_simulated_forecast_timeout_is_read_back_with_remote_write_ahead(self):
        plan=make_plan();post=post_fixture();calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            host=self.host(tmp)
            def send(payload):
                _,files=store(self.git).load()
                self.assertTrue(inspect_database(files['journal.sqlite3'],self.store)['blocked'])
                calls.append(payload);post.update(with_forecast(post,plan));raise TimeoutError()
            try:
                r=submit_once(plan,host.ledger,fetch=lambda pid:(ME,post),post_forecast=host.model_transport(send),clock=lambda:NOW,enabled=True)
                self.assertTrue(r['verified']);self.assertEqual(len(calls),1)
            finally:host.close()
            _,files=store(self.git).load();self.assertFalse(inspect_database(files['journal.sqlite3'],self.store)['blocked'])

if __name__=='__main__':unittest.main()
