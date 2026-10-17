import copy,json,tempfile,unittest
from pathlib import Path
from advisor_policy import OPUS
from bounded_evidence import extract
from operations_ledger_v1 import Ledger,GlobalStop
from candidate_service_v1 import run_bound_question,whole_question_reserve
from competition_adapter_v1 import bind_post
from test_competition_adapter_v1 import post_fixture,ME,NOW

OBS='The supplied observed baseline was 19 units.'
class ServiceTests(unittest.TestCase):
    def execute(self,ledger,folder,calls,*,fail=None):
        b=bind_post(post_fixture(),ME,tournament_id=33121,expected_username='Frogman-bot',now=NOW);saved=[]
        def transport(p):
            calls.append(copy.deepcopy(p));body=json.loads(p['messages'][0]['content'])
            if fail:raise TimeoutError()
            if 'tools' in p:tag='research';a=dict(question_id=body['question']['id'],needs=['Find the historical baseline.'],source_urls=['https://example.org/source'])
            elif '<research_quality>' in p['system']:tag='research_quality';a=dict(findings=[],selected_urls=[])
            elif '<compact_review>' in p['system']:tag='compact_review';a=dict(checks=[dict(fact_index=0,supported=True,evidence=[dict(url='https://example.org/source',quote=OBS)])],unsupported=[],subjective=[])
            else:tag='forecast';a=dict(question_id=body['question']['id'],decision='forecast',distribution=dict(kind='binary',probability=.55),facts=[dict(claim=OBS,url='https://example.org/source',observation_date=None)],assumptions=['Future judgement.'],gaps=[],rationale='The historical baseline supports a modest increase with material future uncertainty.')
            return dict(model=OPUS,stop_reason='end_turn',usage=dict(input_tokens=100,output_tokens=100),content=[dict(type='text',text='<'+tag+'>'+json.dumps(a)+'</'+tag+'>')])
        return run_bound_question(b,folder,ledger,transport=transport,count=lambda p:100,clock=lambda:NOW,save=saved.append,
          capture_initial=lambda q,u,s,f:dict(documents=[extract(u[0],dict(type='text',data=OBS),NOW.isoformat())],failures=[]),
          capture_follow=lambda *a:dict(documents=[],failures=[]),candidates=lambda *a:[]),saved
    def test_full_dry_run_has_plan_and_costs_survive_restart(self):
        self.assertEqual(str(whole_question_reserve()),'2.336')
        calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'state.db';l=Ledger(p,cap_usd='3',period_key='test')
            try:
                r,saved=self.execute(l,tmp,calls)
                self.assertEqual(r['status'],'prepared_local_only');self.assertFalse(r['submission_plan']['submission_enabled'])
                self.assertEqual(r['submission_plan']['payload'][0]['question'],102)
                self.assertEqual(len(calls),4);self.assertEqual(l.db.execute("SELECT COUNT(*) FROM operations WHERE state='settled'").fetchone()[0],4)
                spent=3000000-l.available_units();self.assertGreater(spent,0)
            finally:l.close()
            l=Ledger(p,cap_usd='3',period_key='test')
            try:
                self.assertEqual(3000000-l.available_units(),spent)
                with self.assertRaises(GlobalStop):self.execute(l,tmp,calls)
                self.assertEqual(len(calls),4)
            finally:l.close()
    def test_full_reservation_gate_before_first_model_call(self):
        calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            l=Ledger(Path(tmp)/'state.db',cap_usd='2',period_key='test')
            try:
                with self.assertRaises(GlobalStop):self.execute(l,tmp,calls)
                self.assertEqual(calls,[])
            finally:l.close()
    def test_unknown_operation_retains_session_and_budget_across_restart(self):
        calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'state.db';l=Ledger(p,cap_usd='3',period_key='test')
            try:
                r,_=self.execute(l,tmp,calls,fail=True);self.assertEqual(r['status'],'global_stop')
                self.assertEqual(l.db.execute('SELECT status FROM sessions').fetchone()[0],'active')
            finally:l.close()
            l=Ledger(p,cap_usd='3',period_key='test')
            try:
                with self.assertRaises(GlobalStop):self.execute(l,tmp,calls)
                self.assertEqual(len(calls),1)
            finally:l.close()

if __name__=='__main__':unittest.main()
