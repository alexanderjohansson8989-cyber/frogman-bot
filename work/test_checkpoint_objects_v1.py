import copy,json,sqlite3,tempfile,unittest,zlib
from pathlib import Path
from checkpoint_objects_v1 import initialize,pack,unpack,read_event
from durable_candidate_host_v1 import database_bytes,DurableHost
from git_state_store_v1 import canonical,sha256
from operations_ledger_v1 import GlobalStop
from test_git_state_store_v1 import FakeGit,bootstrap,store,OWNER,NOW


class ObjectTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');initialize(self.db)
        self.db.execute('CREATE TABLE checkpoint_events(sequence INTEGER PRIMARY KEY,created_utc TEXT,action TEXT,archive_sha256 TEXT)')
    def tearDown(self):self.db.close()
    def event(self,obj):
        raw=canonical(obj);root=pack(self.db,obj)
        cursor=self.db.execute('INSERT INTO checkpoint_events(created_utc,action,archive_sha256) VALUES(?,?,?)',
            (obj['created_utc'],obj['action'],sha256(raw)))
        self.db.execute('INSERT INTO checkpoint_roots VALUES(?,?)',(cursor.lastrowid,root))
        return cursor.lastrowid,root
    def test_exact_roundtrip_and_immutable_shared_source(self):
        source={'text':'Åäö\nsource '*20000,'offsets':[0,12], 'missing':None}
        first={'documents':[source],'values':[True,False,0,-1,1.0,-0.0,1e20,'1']}
        h=pack(self.db,first);count=self.db.execute('SELECT COUNT(*) FROM checkpoint_objects').fetchone()[0]
        second=copy.deepcopy(first);second['stage']='forecast';h2=pack(self.db,second)
        self.assertLessEqual(self.db.execute('SELECT COUNT(*) FROM checkpoint_objects').fetchone()[0]-count,2)
        self.assertEqual(canonical(unpack(self.db,h)),canonical(first))
        self.assertEqual(canonical(unpack(self.db,h2)),canonical(second))
        first['documents'][0]['text']='modified caller'
        self.assertNotEqual(unpack(self.db,h),first)
    def test_event_hash_and_metadata_verified(self):
        e=dict(created_utc='2026-10-18T12:00:00Z',action='pipeline-result',arguments={'test':'value'})
        seq,root=self.event(e);self.assertEqual(read_event(self.db,seq,{}),e)
        self.db.execute('UPDATE checkpoint_events SET action=? WHERE sequence=?',('wrong',seq))
        with self.assertRaises(GlobalStop):read_event(self.db,seq,{})
    def test_legacy_read_keeps_original_bytes(self):
        e=dict(created_utc='2026-10-18T12:00:00Z',action='pipeline-result',arguments={'test':'value'})
        raw=canonical(e);h=sha256(raw);archive={'a-'+h+'.json':raw}
        self.db.execute('INSERT INTO checkpoint_events VALUES(1,?,?,?)',(e['created_utc'],e['action'],h))
        self.assertEqual(read_event(self.db,1,archive),e)
        self.assertEqual(archive,{'a-'+h+'.json':raw})
        with self.assertRaises(GlobalStop):read_event(self.db,1,{})
    def test_corrupt_missing_and_trailing_compressed_objects_stop(self):
        h=pack(self.db,{'x':'test'})
        raw=self.db.execute('SELECT compressed FROM checkpoint_objects WHERE sha256=?',(h,)).fetchone()[0]
        for bad in (b'broken',raw+b'extra',zlib.compress(b'["scalar",42]')):
            self.db.execute('UPDATE checkpoint_objects SET compressed=? WHERE sha256=?',(bad,h))
            with self.assertRaises(GlobalStop):unpack(self.db,h)
        self.db.execute('DELETE FROM checkpoint_objects WHERE sha256=?',(h,))
        with self.assertRaises(GlobalStop):unpack(self.db,h)
    def test_conflict_never_overwrites_existing_corrupt_object(self):
        h=pack(self.db,'value');self.db.execute('UPDATE checkpoint_objects SET compressed=? WHERE sha256=?',(b'bad',h))
        with self.assertRaises(GlobalStop):pack(self.db,'value')
        self.assertEqual(self.db.execute('SELECT compressed FROM checkpoint_objects WHERE sha256=?',(h,)).fetchone()[0],b'bad')
    def test_unknown_version_refused(self):
        self.db.execute("UPDATE checkpoint_codec SET version='future'")
        with self.assertRaises(GlobalStop):initialize(self.db)
    def test_transaction_rollback_retains_no_partial_event(self):
        self.db.commit()
        with self.assertRaises(RuntimeError):
            with self.db:
                self.event(dict(created_utc='now',action='test',arguments={'large':'abc'*1000}))
                raise RuntimeError('interrupt')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM checkpoint_roots').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM checkpoint_objects').fetchone()[0],0)
    def test_depth_limit(self):
        obj='value'
        for i in range(105):obj=[obj]
        with self.assertRaises(GlobalStop):pack(self.db,obj)
    def test_real_host_reopens_without_event_json_and_preserves_budget(self):
        git=FakeGit();bootstrap(git)
        with tempfile.TemporaryDirectory() as tmp:
            def host(path):return DurableHost(store(git),OWNER,path,clock=lambda:NOW,
                credit_expires_utc='2026-10-21T00:00:00Z',key_expires_utc='2026-11-01T00:00:00Z')
            a=host(Path(tmp)/'a')
            a.ledger.reserve('one','need',{'prompt':'test'},'.2');a.ledger.dispatch('one',{'prompt':'test'})
            a.ledger.save_raw('one',{'usage':1});a.ledger.settle('one','.1',{'usage':1})
            a.save({'documents':[{'text':'source'*1000}]});a.close()
            self.assertEqual(set(store(git).load()[1]),{'journal.sqlite3'})
            b=host(Path(tmp)/'b')
            self.assertEqual(b.ledger.available_units(),2900000)
            events=b.ledger.db.execute('SELECT sequence FROM checkpoint_events').fetchall()
            decoded=[read_event(b.ledger.db,n,{}) for (n,) in events]
            self.assertTrue(any(e['action']=='pipeline-result' for e in decoded))
            with self.assertRaises(GlobalStop):b.ledger.reserve('one','need',{},'.1')
            b.close()

if __name__=='__main__':unittest.main()
