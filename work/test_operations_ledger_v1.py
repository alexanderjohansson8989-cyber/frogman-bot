import copy,json,tempfile,unittest
from decimal import Decimal
from pathlib import Path
from advisor_policy import OPUS
from operations_ledger_v1 import Ledger,GlobalStop,ReservedRequester
from candidate_profile_v1 import OUT

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ledger.sqlite3'
        self.l=Ledger(self.path,cap_usd='2',period_key='test-period')
    def tearDown(self):self.l.close();self.temp.cleanup()
    def test_budget_duplicate_and_policy_survive_reopen(self):
        self.l.reserve('first','need',{'a':1},'1.20');self.l.dispatch('first',{'a':1})
        raw={'received':True};self.l.save_raw('first',raw);self.l.settle('first','0.40',raw)
        self.l.close();self.l=Ledger(self.path,cap_usd='2',period_key='test-period')
        self.assertEqual(self.l.available_units(),1600000)
        with self.assertRaises(GlobalStop):self.l.reserve('first','need',{'a':1},'.1')
        with self.assertRaises(GlobalStop):self.l.reserve('other','need',{},'1.61')
        with self.assertRaises(GlobalStop):Ledger(self.path,cap_usd='20',period_key='test-period')
    def test_uncertain_blocks_other_connection_without_spending_more(self):
        self.l.reserve('one','need',{},'1');self.l.dispatch('one',{})
        other=Ledger(self.path,cap_usd='2',period_key='test-period')
        try:
            with self.assertRaises(GlobalStop):other.reserve('two','need',{},'.1')
        finally:other.close()
        self.assertEqual(self.l.available_units(),1000000)
    def test_changed_receipt_and_payload_cannot_reconcile(self):
        self.l.reserve('one','need',{'a':1},'1')
        with self.assertRaises(GlobalStop):self.l.dispatch('one',{'a':2})
        self.l.dispatch('one',{'a':1});self.l.save_raw('one',{'usage':1})
        with self.assertRaises(GlobalStop):self.l.settle('one','.1',{'usage':2})
        self.assertEqual(self.l.available_units(),1000000)
    def test_overrun_commits_actual_and_persists_stop(self):
        self.l.reserve('one','need',{},'.1');self.l.dispatch('one',{});self.l.save_raw('one',{})
        with self.assertRaises(GlobalStop):self.l.settle('one','.2',{})
        self.assertEqual(self.l.available_units(),1800000)
        with self.assertRaises(GlobalStop):self.l.reserve('two','need',{},'.1')
    def requester(self,transport,count=lambda p:100):
        return ReservedRequester(self.l,'new-q',transport=transport,count=count,guard=lambda:None)
    def payload(self):return dict(model=OPUS,system='Test',messages=[dict(role='user',content='Public source text')],max_tokens=OUT['need'],output_config={'effort':'medium'})
    def test_transport_timeout_never_retries_and_retains_reservation(self):
        calls=[]
        def fail(p):calls.append(p);raise TimeoutError()
        req=self.requester(fail)
        with self.assertRaises(GlobalStop):req('need',self.payload())
        with self.assertRaises(GlobalStop):req('need',self.payload())
        self.assertEqual(len(calls),1)
    def test_raw_unknown_usage_saved_before_stop(self):
        raw=dict(model=OPUS,usage={},stop_reason='end_turn',content=[])
        with self.assertRaises(GlobalStop):self.requester(lambda p:raw)('need',self.payload())
        stored=self.l.db.execute('SELECT raw,state FROM operations').fetchone()
        self.assertEqual(json.loads(stored[0]),raw);self.assertEqual(stored[1],'uncertain')
    def test_completed_usage_settles_even_if_generation_truncated(self):
        raw=dict(model=OPUS,usage=dict(input_tokens=100,output_tokens=100),stop_reason='max_tokens',content=[])
        self.assertEqual(self.requester(lambda p:raw)('need',self.payload()),raw)
        self.assertEqual(self.l.db.execute('SELECT state FROM operations').fetchone()[0],'settled')
    def test_second_need_refused_before_transport(self):
        calls=[]
        raw=dict(model=OPUS,usage=dict(input_tokens=100,output_tokens=100),stop_reason='end_turn',content=[])
        req=self.requester(lambda p:(calls.append(p),raw)[1])
        req('need',self.payload())
        with self.assertRaises(GlobalStop):req('need',self.payload())
        self.assertEqual(len(calls),1)
    def test_expired_after_count_prevents_dispatch(self):
        calls=[];state=[False]
        def count(p):state[0]=True;return 100
        def guard():
            if state[0]:raise GlobalStop('Expired')
        req=ReservedRequester(self.l,'q',transport=calls.append,count=count,guard=guard)
        with self.assertRaises(GlobalStop):req('need',self.payload())
        self.assertEqual(calls,[])

if __name__=='__main__':unittest.main()
