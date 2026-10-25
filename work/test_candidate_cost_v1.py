import copy,json,tempfile,unittest
from pathlib import Path
from decimal import Decimal
from advisor_policy import OPUS
from candidate_cost_v1 import cost
from candidate_profile_v1 import OUT,EFFORT
from operations_ledger_v1 import Ledger,ReservedRequester,GlobalStop,units
from candidate_recovery_v1 import RecoveryHost,inspect
from test_git_state_store_v1 import FakeGit,bootstrap,store,OTHER
from test_candidate_runtime_v1 import new_host,terminal
from test_competition_adapter_v1 import NOW
from independent_runner import Stop

def payload(stage):
    p=dict(model=OPUS,system='Synthetic',messages=[dict(role='user',content='Synthetic')],
        max_tokens=OUT[stage],output_config=dict(effort=EFFORT[stage]))
    if stage=='search':p['tools']=[dict(type='web_search_20250305',name='web_search',max_uses=2)]
    return p

def usage(out):return dict(model=OPUS,usage=dict(input_tokens=1,output_tokens=out),stop_reason='end_turn',content=[])

class CandidateCostTests(unittest.TestCase):
    def test_each_stage_records_overrun_then_stops_without_retry(self):
        for stage in OUT:
            for out in (OUT[stage]+1,12001):
                with self.subTest(stage=stage,out=out),tempfile.TemporaryDirectory() as tmp:
                    l=Ledger(Path(tmp)/'db',cap_usd='3',period_key='test');calls=[];raw=usage(out)
                    def transport(p):calls.append(p);return raw
                    req=ReservedRequester(l,'q',transport=transport,count=lambda _:10000,guard=lambda:None)
                    with self.assertRaises(GlobalStop):req(stage,payload(stage))
                    state,actual,saved=l.db.execute('SELECT state,actual,raw FROM operations').fetchone()
                    self.assertEqual((state,actual),('settled',4+20*out));self.assertEqual(json.loads(saved),raw)
                    self.assertEqual(l.db.execute('SELECT stopped FROM policy').fetchone()[0],1)
                    with self.assertRaises(GlobalStop):req(stage,payload(stage))
                    self.assertEqual(len(calls),1);l.close()

    def test_recovery_each_stage_settles_and_stops_in_one_checkpoint(self):
        for stage in OUT:
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as tmp:
                git=FakeGit();bootstrap(git);h=new_host(git,Path(tmp)/'old');p=payload(stage);raw=usage(OUT[stage]+1)
                key='q/'+stage+'/0';reserve='1.2' if stage=='search' else str((Decimal(12000)*4+Decimal(OUT[stage])*20)/1000000)
                h.ledger.reserve(key,stage,p,reserve);h.ledger.dispatch(key,p);h.ledger.save_raw(key,raw)
                with self.assertRaises(GlobalStop):h.close()
                r=RecoveryHost(store(git),OTHER,Path(tmp)/'recover',get_run=terminal,clock=lambda:NOW)
                before=r.store.manifest['sequence']
                with self.assertRaises(GlobalStop):r.settle_saved()
                self.assertEqual(r.store.manifest['sequence'],before+1)
                self.assertEqual(r.ledger.db.execute('SELECT state,actual FROM operations').fetchone(),('settled',4+20*(OUT[stage]+1)))
                self.assertEqual(r.ledger.db.execute('SELECT stopped FROM policy').fetchone()[0],1)
                r.close();self.assertTrue(inspect(store(git))['blocked'])

    def test_current_caps_and_known_search_cost_keep_same_accounting(self):
        from prospective_runner_4 import cost as historical_cost
        for stage in OUT:
            for out in (0,OUT[stage]):
                raw=usage(out)
                if stage=='search':raw['usage']['server_tool_use']={'web_search_requests':2}
                self.assertEqual(cost(raw,OPUS,stage),historical_cost(raw,OPUS,stage))
                with tempfile.TemporaryDirectory() as tmp:
                    l=Ledger(Path(tmp)/'db',cap_usd='3',period_key='test')
                    req=ReservedRequester(l,'q',transport=lambda _:raw,count=lambda _:10000,guard=lambda:None)
                    self.assertEqual(req(stage,payload(stage)),raw)
                    self.assertEqual(l.db.execute('SELECT stopped FROM policy').fetchone()[0],0);l.close()

    def test_unknown_usage_stays_reserved_direct_and_during_recovery(self):
        variants=[]
        for field,value in [('output_tokens',True),('output_tokens',200001),('output_tokens',-1),
                            ('cache_read_input_tokens',3),('service_tier','priority'),
                            ('server_tool_use',{'web_search_requests':1}),('iterations',[{}])]:
            raw=usage(100);raw['usage'][field]=value;variants.append(raw)
        for raw in variants:
            with self.subTest(usage=raw['usage']),tempfile.TemporaryDirectory() as tmp:
                git=FakeGit();bootstrap(git);h=new_host(git,Path(tmp)/'old');p=payload('need')
                req=ReservedRequester(h.ledger,'q',transport=lambda _:raw,count=lambda _:10000,guard=h.before_effect)
                with self.assertRaisesRegex(GlobalStop,'Usage unknown'):req('need',p)
                self.assertEqual(h.ledger.db.execute('SELECT state,actual FROM operations').fetchone(),('uncertain',None))
                with self.assertRaises(GlobalStop):h.close()
                r=RecoveryHost(store(git),OTHER,Path(tmp)/'recover',get_run=terminal,clock=lambda:NOW)
                self.assertEqual(r.settle_saved(),dict(settled=[],unknown=['q/need/0']));r.close()

    def test_historical_experiment_contract_is_not_relaxed(self):
        from prospective_runner_4 import cost as historical_cost
        for stage in OUT:
            with self.assertRaises(Stop):historical_cost(usage(12001),OPUS,stage)
            self.assertEqual(cost(usage(12001),OPUS,stage),Decimal('0.240024'))

if __name__=='__main__':unittest.main()
