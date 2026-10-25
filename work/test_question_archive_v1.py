import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from checkpoint_objects_v1 import events_from_snapshot
from question_archive_v1 import compact_completed,load_archived_session
from operations_ledger_v1 import GlobalStop,ReservedRequester
from test_git_state_store_v1 import FakeGit,bootstrap,store,OWNER,NOW
from test_candidate_runtime_v1 import new_host,need,raw,FakeHTTP
from candidate_api_v1 import ClaudeClient
from candidate_dispatch_v1 import model_transport
from bounded_evidence import digest


def complete(host,key='question-1'):
    l=host.ledger;l.start_session(key,{'question':key},'1')
    request=ReservedRequester(l,key,transport=model_transport(host,ClaudeClient('synthetic',opener=FakeHTTP([raw()]))),
        count=lambda p:100,guard=host.before_effect)
    request('need',need())
    result={'status':'prepared_local_only','documents':[{'text':'source '*1000}],'question':key}
    host.save(result);l.finish_session(key,result)
    return result


class ArchiveTests(unittest.TestCase):
    def setUp(self):self.git=FakeGit();bootstrap(self.git)
    def test_archive_reopen_exact_result_cost_and_permit_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=new_host(self.git,Path(tmp)/'a');result=complete(a)
            amount=a.ledger.available_units();permits=a.ledger.db.execute('SELECT * FROM effect_permits').fetchall()
            pointer=compact_completed(a)
            self.assertEqual(a.ledger.available_units(),amount)
            self.assertEqual(set(a.store.files),{'journal.sqlite3'})
            self.assertEqual(a.ledger.db.execute('SELECT raw FROM operations').fetchall(),[(None,)])
            self.assertEqual(a.ledger.db.execute('SELECT * FROM effect_permits').fetchall(),permits)
            self.assertIsNone(compact_completed(a));a.close()
            b=new_host(self.git,Path(tmp)/'b')
            restored=load_archived_session(b.store,b.ledger.db,'question-1')
            self.assertEqual(restored['result'],result)
            self.assertEqual(restored['manifest']['files']['journal.sqlite3']['sha256'],
                __import__('hashlib').sha256(restored['files']['journal.sqlite3']).hexdigest())
            self.assertEqual(b.ledger.available_units(),amount)
            with self.assertRaises(GlobalStop):b.ledger.start_session('question-1',{},'1')
            self.assertEqual(b.ledger.db.execute('SELECT * FROM effect_permits').fetchall(),permits)
            b.close()
    def test_two_archives_restore_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);first=complete(h,'first');p1=compact_completed(h)
            second=complete(h,'second');p2=compact_completed(h)
            self.assertNotEqual(p1['commit_sha'],p2['commit_sha'])
            self.assertEqual(load_archived_session(h.store,h.ledger.db,'first')['result'],first)
            self.assertEqual(load_archived_session(h.store,h.ledger.db,'second')['result'],second)
            self.assertEqual(h.ledger.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],2)
            h.close()
    def test_unknown_model_and_pending_submission_cannot_be_archived(self):
        for mode in ('model','submission'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                git=FakeGit();bootstrap(git);h=new_host(git,tmp);complete(h)
                if mode=='model':h.ledger.reserve('unknown','need',{},'.1');h.ledger.dispatch('unknown',{})
                else:h.ledger.prepare_submission('pending',{'test':True})
                head=h.store.head;files=copy.deepcopy(h.store.files)
                with self.assertRaises(GlobalStop):compact_completed(h)
                self.assertEqual(h.store.head,head);self.assertEqual(h.store.files,files)
                if mode=='model':
                    with self.assertRaises(GlobalStop):h.close()
                else:h.close()
    def test_failed_compaction_publication_leaves_original_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);result=complete(h);old=h.store.head
            self.git.fail='before'
            with __import__('unittest').mock.patch('git_state_store_v1.time.sleep'),self.assertRaises(GlobalStop):compact_completed(h)
            self.git.fail=None
            fresh=store(self.git);m,files=fresh.load()
            self.assertEqual(fresh.head,old);self.assertTrue(h.store.poisoned)
            self.assertTrue(any(e['action']=='session-finish' and e['arguments'][1]==result for e in events_from_snapshot(files)))
            with self.assertRaises(GlobalStop):h.before_effect()
            with self.assertRaises(GlobalStop):h.close()
    def test_archive_receipt_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);complete(h);compact_completed(h)
            h.ledger.db.execute("UPDATE question_archives SET manifest_sha256=?",('0'*64,))
            with self.assertRaises(GlobalStop):load_archived_session(h.store,h.ledger.db,'question-1')
            # Test-only local tampering is not published.
            h.ledger.close()


class BatchTests(unittest.TestCase):
    def setUp(self):self.git=FakeGit();bootstrap(self.git)
    def test_batch_has_one_remote_commit_and_blocks_effect_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);before=len(self.git.updated)
            with h.ledger.checkpoint_batch():
                h.ledger.reserve('q','need',{},'.1');h.ledger.dispatch('q',{})
                self.assertEqual(len(self.git.updated),before)
                with self.assertRaises(GlobalStop):h.before_effect()
            self.assertEqual(len(self.git.updated),before+1)
            with self.assertRaises(GlobalStop):h.close()
    def test_exception_still_publishes_saved_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);h.ledger.reserve('q','need',{},'.1');h.ledger.dispatch('q',{})
            with self.assertRaises(ValueError):
                with h.ledger.checkpoint_batch():
                    h.ledger.save_raw('q',{'usage':'unknown'})
                    raise ValueError('cost parser failed')
            db=sqlite3.connect(':memory:');db.deserialize(store(self.git).load()[1]['journal.sqlite3'])
            self.assertEqual(json.loads(db.execute('SELECT raw FROM operations').fetchone()[0]),{'usage':'unknown'})
            db.close()
            with self.assertRaises(GlobalStop):h.close()
    def test_overrun_stop_survives_batch_and_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=new_host(self.git,tmp);h.ledger.reserve('q','need',{},'.1');h.ledger.dispatch('q',{})
            with self.assertRaises(GlobalStop):
                with h.ledger.checkpoint_batch():h.ledger.save_raw('q',{});h.ledger.settle('q','.2',{})
            db=sqlite3.connect(':memory:');db.deserialize(store(self.git).load()[1]['journal.sqlite3'])
            self.assertEqual(db.execute('SELECT stopped FROM policy').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT state,actual FROM operations').fetchone(),('settled',200000))
            db.close()
            with self.assertRaises(GlobalStop):h.close()

if __name__=='__main__':unittest.main()
