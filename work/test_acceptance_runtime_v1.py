import copy,datetime as dt,json,tempfile,unittest
from test_candidate_runtime_v1 import FakeHTTP,new_host
from test_git_state_store_v1 import FakeGit,bootstrap,store
from test_competition_adapter_v1 import NOW
from test_test_area_acceptance_v1 import fixture,plan
from test_area_acceptance_v1 import ACTOR
from acceptance_runtime_v1 import execute
from candidate_api_v1 import MetaculusClient
from operations_ledger_v1 import GlobalStop
from candidate_recovery_v1 import inspect
from acceptance_driver_v1 import run as run_driver, BRANCH
from pathlib import Path


def forecast(p):
    q=fixture();q['question']['my_forecasts']['latest']=dict(author_id=ACTOR['id'],question_id=102,
        start_time=NOW.timestamp(),end_time=None,forecast_values=[.5,.5])
    return q


def comment(p):
    return dict(id=42,on_post=101,author=ACTOR,is_private=True,is_soft_deleted=False,parent_id=None,
        text=p['comment']['text'],created_at=NOW.isoformat(),
        included_forecast=dict(start_time=NOW.isoformat(),probability_yes=.5,
            probability_yes_per_category=None,continuous_cdf=None,quartiles=None,
            scaling=dict(range_min=None,range_max=None,zero_point=None),options=None,
            question_type='binary',question_unit=''))


class AcceptanceRuntimeTests(unittest.TestCase):
    @unittest.skip('Private historical fixture excluded from public package; tested separately in private CI')
    def test_original_authenticated_contract_replays_without_network(self):
        from test_area_acceptance_v1 import preflight
        original=json.loads((Path(__file__).resolve().parents[1]/'outputs/korningar/acceptance_preflight_20261022/live_result.json').read_bytes())
        p=original['plan']
        self.assertTrue(preflight(p,original['selected_input'],original['actor'],
            listing_post_ids=original['listing_ids'],now=dt.datetime.fromisoformat(p['binding']['bound_at_utc'])))
        self.assertEqual(p['binding']['post_id'],43327)
        self.assertEqual(p['payload'][0]['question'],43332)

    def test_fixed_branch_prevents_second_workflow_dispatch(self):
        p=plan();git=FakeGit()
        net=FakeHTTP([ACTOR,{'results':[fixture()]},fixture(),{},ACTOR,forecast(p),{},
                      {'results':[comment(p)],'next':None}])
        with tempfile.TemporaryDirectory() as tmp:
            def go(name,runid):
                return run_driver(p,git,MetaculusClient('synthetic-test-secret',opener=net),Path(tmp)/name,
                    approved_plan_sha256=p['plan_sha256'],run_id=runid,attempt=1,code_sha256='a'*64,clock=lambda:NOW)
            self.assertEqual(go('first','123')['status'],'verified')
            self.assertIn(BRANCH,git.refs)
            before=len(net.calls)
            with self.assertRaises(GlobalStop):go('again','124')
            self.assertEqual(len(net.calls),before)

    def test_unapproved_has_no_io_or_journal_access(self):
        with self.assertRaises(GlobalStop):execute(plan(),None,None)

    def run_case(self,net,p,*,success):
        git=FakeGit();bootstrap(git)
        with tempfile.TemporaryDirectory() as tmp:
            host=new_host(git,tmp);client=MetaculusClient('synthetic-test-secret',opener=net)
            if success:
                result=execute(p,host,client,approved_plan_sha256=p['plan_sha256'])
                self.assertTrue(result['forecast']['verified']);self.assertTrue(result['comment']['private'])
                self.assertEqual(host.ledger.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)
                host.close();self.assertFalse(inspect(store(git))['blocked'])
            else:
                with self.assertRaises(GlobalStop):execute(p,host,client,approved_plan_sha256=p['plan_sha256'])
                with self.assertRaises(GlobalStop):host.close()
                self.assertTrue(inspect(store(git))['blocked'])
        return net.calls

    def test_two_single_posts_and_independent_readbacks_without_ai(self):
        p=plan();net=FakeHTTP([ACTOR,{'results':[fixture()]},fixture(),{},ACTOR,forecast(p),{},
                              {'results':[comment(p)],'next':None}])
        calls=self.run_case(net,p,success=True)
        posts=[x for x in calls if x[1]=='POST']
        self.assertEqual([x[0].split('/api/')[1] for x in posts],['questions/forecast/','comments/create/'])
        self.assertTrue(posts[1][2]['is_private'])

    def test_countless_next_link_on_short_final_page_does_not_loop(self):
        p=plan();net=FakeHTTP([ACTOR,{'results':[fixture()]},fixture(),{},ACTOR,forecast(p),{},
                              {'results':[comment(p)],'next':'server-always-supplies-next'}])
        calls=self.run_case(net,p,success=True)
        self.assertEqual(sum('/comments/?' in x[0] for x in calls),1)

    def test_full_page_of_nonmatches_does_not_end_pagination(self):
        p=plan();bad=comment(p);bad['included_forecast']['start_time']=(NOW-dt.timedelta(seconds=.5)).isoformat()
        net=FakeHTTP([ACTOR,{'results':[fixture()]},fixture(),{},ACTOR,forecast(p),{},
                      {'results':[copy.deepcopy(bad) for _ in range(25)],'next':'next'},
                      {'results':[comment(p)],'next':'next'}])
        calls=self.run_case(net,p,success=True)
        self.assertEqual(sum('/comments/?' in x[0] for x in calls),2)

    def test_oversized_comment_page_is_not_hidden_by_filtering(self):
        p=plan();net=FakeHTTP([ACTOR,{'results':[fixture()]},fixture(),{},ACTOR,forecast(p),{},
                              {'results':[comment(p) for _ in range(26)],'next':None}])
        self.run_case(net,p,success=False)

    def test_timeout_after_acceptance_reads_back_without_resending(self):
        p=plan();net=FakeHTTP([ACTOR,{'results':[fixture()]},fixture(),TimeoutError(),ACTOR,forecast(p),TimeoutError(),
                              {'results':[comment(p)],'next':None}])
        calls=self.run_case(net,p,success=True)
        self.assertEqual(sum(x[1]=='POST' for x in calls),2)

    def test_unconfirmed_forecast_blocks_comment_and_preserves_uncertainty(self):
        p=plan();net=FakeHTTP([ACTOR,{'results':[fixture()]},fixture(),TimeoutError(),ACTOR,fixture()])
        calls=self.run_case(net,p,success=False)
        self.assertEqual(sum(x[1]=='POST' for x in calls),1)

    def test_wrong_attached_forecast_cannot_confirm_comment(self):
        p=plan();bad=comment(p);bad['included_forecast']['start_time']=(NOW-dt.timedelta(seconds=.5)).isoformat()
        net=FakeHTTP([ACTOR,{'results':[fixture()]},fixture(),{},ACTOR,forecast(p),{},
                      {'results':[bad],'next':None}])
        calls=self.run_case(net,p,success=False)
        self.assertEqual(sum(x[1]=='POST' for x in calls),2)

if __name__=='__main__':unittest.main()
