"""One frozen live question through the durable host, WITHOUT submission."""
from independent_runner import NoRedirect as SourceNoRedirect
import datetime as dt,json,os,secrets
from decimal import Decimal
from pathlib import Path
from advisor_policy import OPUS,RATES
from bounded_evidence import digest
from candidate_api_v1 import ClaudeClient,MetaculusClient
from candidate_dispatch_v1 import model_transport
from candidate_service_v1 import whole_question_reserve
from competition_adapter_v1 import bind_post
from durable_candidate_host_v1 import DurableHost,database_bytes
from general_evaluation import instant
from git_state_store_v1 import SnapshotStore,owner_id
from github_git_transport_v1 import GitHubGit
from operations_ledger_v1 import Ledger,GlobalStop
from research_experiment_3 import candidates
from research_quality import capture_children
from text_source_capture import capture_selected

REPO='alexanderjohansson8989-cyber/metaculus-bot'
def validate_input(p,now):
    if (p.get('version')!='candidate-dry-run-v1' or p.get('publication') is not False
            or p.get('cap_usd')!='3' or p.get('rates')!={OPUS:['4','20'],'search_request':'.01'}
            or [str(v) for v in RATES[OPUS]]!=p['rates'][OPUS]):raise GlobalStop('Frozen dry-run contract')
    balance=p.get('console_balance_usd');observed=p.get('console_observed_utc')
    if not isinstance(balance,str) or not Decimal(balance).is_finite() or Decimal(balance)<Decimal(p['cap_usd']) or not observed:
        raise GlobalStop('Current shared-credit balance required before live start')
    if not instant(observed)<=now<instant(observed)+dt.timedelta(hours=1):raise GlobalStop('Credit observation is stale')
    cutoff=min(instant(p['binding']['cutoff_utc']),instant(p['credit_expires_utc']),instant(p['key_expires_utc']))
    if now>=cutoff:raise GlobalStop('Frozen question or credit/key window expired')
    if whole_question_reserve()>Decimal(p['cap_usd']):raise GlobalStop('Complete question does not fit')

def run(p,git,mc,claude,folder,*,run_id,attempt,code_sha256,clock,capture_initial=None,capture_follow=None):
    now=clock();validate_input(p,now);owner=owner_id(run_id,attempt,secrets.token_hex(16))
    me,post=mc.fetch(p['binding']['post_id'])
    b=bind_post(post,me,tournament_id=33121,expected_username='Frogman-bot',now=clock())
    old=p['binding']
    if (b['actor']!=old['actor'] or b['post_id']!=old['post_id'] or b['question_id']!=old['question_id']
            or b['question_contract_sha256']!=old['question_contract_sha256'] or b['cutoff_utc']!=old['cutoff_utc']):
        raise GlobalStop('Live question changed since binding')
    if claude.metadata().get('id')!=OPUS:raise GlobalStop('Current model access differs')
    # Explicitly isolated pilot state. Never initialize the production branch
    # with synthetic tests or overwrite a prior attempted question.
    # One fixed bootstrap-only branch makes this first pilot single-use even
    # if another push creates a different workflow run ID for the same question.
    store=SnapshotStore(git,repository=REPO,branch='frogman-state-dryrun-first-v1',
        period='candidate-dryrun-first-v1',cap_units=3000000,code_sha256=code_sha256,clock=clock)
    dest=Path(folder);dest.mkdir(parents=True,exist_ok=True)
    initial=Ledger(dest/'initial.sqlite3',cap_usd=p['cap_usd'],period_key=store.period)
    try:store.bootstrap({'journal.sqlite3':database_bytes(initial)})
    finally:initial.close()
    host=DurableHost(store,owner,dest/'restored',clock=clock,credit_expires_utc=p['credit_expires_utc'],key_expires_utc=p['key_expires_utc'])
    try:
        host.ledger.record('frozen-dryrun-input',p)
        # Guard every individual source HTTP read, not only the capture batch.
        import urllib.request
        from candidate_api_v1 import NoRedirect
        opener=urllib.request.build_opener(SourceNoRedirect())
        def source_open(req,**kwargs):host.before_effect();return opener.open(req,**kwargs)
        def initial_capture(q,urls,searched,d):return capture_selected(q,urls,searched,d,open_request=source_open)
        def child_capture(offered,selected,docs,d):return capture_children(offered,selected,docs,d,open_request=source_open)
        result=host.run(b,transport=model_transport(host,claude),count=claude.count,
            capture_initial=capture_initial or initial_capture,capture_follow=capture_follow or child_capture,candidates=candidates)
        summary=dict(version='candidate-dry-run-result-v1',run_id=run_id,status=result['status'],
            post_id=b['post_id'],question_id=b['question_id'],question_contract_sha256=b['question_contract_sha256'],
            journal_operations=host.ledger.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],
            settled_calls=host.ledger.db.execute("SELECT COUNT(*) FROM operations WHERE state='settled'").fetchone()[0],
            actual_cost_units=host.ledger.db.execute('SELECT COALESCE(SUM(actual),0) FROM operations').fetchone()[0],
            state_branch=store.branch,state_head=store.head,code_sha256=code_sha256,
            publication=False,forecast_posts=0,comment_posts=0,diagnostics=result.get('diagnostics',[]))
    finally:host.close()
    summary['state_head']=store.head;summary['owner_released']=store.manifest['owner'] is None
    return summary

def main():
    if os.environ.get('GITHUB_REPOSITORY')!=REPO:raise GlobalStop('Dry-run repository mismatch')
    p=json.loads(Path(os.environ['FROGMAN_INPUT']).read_bytes())
    if digest(p)!=os.environ['FROGMAN_INPUT_SHA']:raise GlobalStop('Frozen input changed')
    result=run(p,GitHubGit(REPO,os.environ['GITHUB_TOKEN']),MetaculusClient(os.environ['METACULUS_TOKEN']),
        ClaudeClient(os.environ['ANTHROPIC_API_KEY']),Path(os.environ['RUNNER_TEMP'])/'candidate-live-dryrun',
        run_id=os.environ['GITHUB_RUN_ID'],attempt=int(os.environ['GITHUB_RUN_ATTEMPT']),
        code_sha256=os.environ['FROGMAN_BUNDLE_SHA'],clock=lambda:dt.datetime.now(dt.timezone.utc))
    print('CANDIDATE_DRYRUN_RESULT '+json.dumps(result,ensure_ascii=False,sort_keys=True))
if __name__=='__main__':main()
