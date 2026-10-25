"""Four real test-area chains, durable journal and timings; no submission API.

Fixed bootstrap-only branch and one shared budget across all four questions.
The diagnostic exception for past spot times is explicit and cannot be submitted.
"""
import datetime as dt,json,os,secrets,time,urllib.request
from decimal import Decimal
from pathlib import Path
from candidate_api_v1 import ClaudeClient,MetaculusClient,NoRedirect
from candidate_dispatch_v1 import model_transport
from competition_adapter_v1 import bind_test_post
from candidate_service_v1 import whole_question_reserve
from durable_candidate_host_v1 import DurableHost,database_bytes
from git_timing_v1 import TimedGit,TimedSnapshotStore
from git_state_store_v1 import owner_id
from github_git_transport_v1 import GitHubGit
from operations_ledger_v1 import Ledger,GlobalStop
from bounded_evidence import digest
from general_evaluation import instant
from advisor_policy import OPUS,RATES
from research_experiment_3 import candidates
from research_quality import capture_children
from text_source_capture import capture_selected
from fourtype_zero_effects_v1 import verify_zero_effects
from independent_runner import NoRedirect as SourceNoRedirect

REPO='alexanderjohansson8989-cyber/metaculus-bot'
BRANCH='frogman-state-fourtype-dryrun-v3'
LISTING='posts/?tournaments=bot-testing-area&forecast_type=binary,numeric,discrete,multiple_choice&statuses=open&limit=100&include_descriptions=true'
KINDS=('binary','multiple_choice','numeric','discrete')

def validate_input(p,now):
    if p.get('version')!='fourtype-dryrun-v2' or p.get('publication') is not False or p.get('cap_usd')!='9':
        raise GlobalStop('Frozen four-type budget contract')
    if p.get('rates')!={OPUS:['4','20'],'search_request':'.01'} or [str(x) for x in RATES[OPUS]]!=['4','20']:
        raise GlobalStop('Price assumptions changed')
    if not instant(p['console_observed_utc'])<=now<instant(p['console_observed_utc'])+dt.timedelta(hours=1):
        raise GlobalStop('Fresh shared balance required')
    balance=Decimal(p['console_balance_usd'])
    if not balance.is_finite() or balance<9 or 4*whole_question_reserve()>9:raise GlobalStop('Four question reserve unavailable')
    if set(p['posts'])!=set(KINDS) or any(type(p['posts'][k]) is not int for k in KINDS):raise GlobalStop('Four fixed post IDs required')
    if now>=min(instant(p['credit_expires_utc']),instant(p['key_expires_utc'])):raise GlobalStop('Credits or key expired')

class ReadOnlyMetaculus(MetaculusClient):
    def __init__(self,*a,**kw):
        super().__init__(*a,**kw);call=self.http.call
        def read(path,*,method='GET',**kwargs):
            if method!='GET' or not (path=='users/me/' or path.startswith('posts/')):
                raise GlobalStop('Four-type probe forbids all Metaculus writes')
            return call(path,method=method,**kwargs)
        self.http.call=read

class TimedOpener:
    def __init__(self,events):self.events=events;self.inner=urllib.request.build_opener(NoRedirect())
    def open(self,req,**kwargs):
        start=time.perf_counter();events=self.events
        label=req.full_url.split('/v1/',1)[-1]
        try:response=self.inner.open(req,**kwargs)
        except BaseException as exc:
            events.append(dict(kind='claude-http',endpoint=label,seconds=time.perf_counter()-start,error_type=type(exc).__name__));raise
        class Response:
            def __enter__(self):return response.__enter__()
            def __exit__(self,*args):
                try:return response.__exit__(*args)
                finally:events.append(dict(kind='claude-http',endpoint=label,seconds=time.perf_counter()-start,error_type=args[0].__name__ if args[0] else None))
        return Response()

def run(p,git,mc,claude,folder,*,run_id,attempt,code_sha256,clock,events,
        capture_initial=None,capture_follow=None,source_candidates=candidates,require_rate=True,verify_previous=verify_zero_effects,
        state_branch=BRANCH,state_period='fourtype-dryrun-v2',prior_label='zero_effects_replacement',archive_finished=False):
    validate_input(p,clock());summary=dict(version='fourtype-dryrun-v2',run_id=run_id,code_sha256=code_sha256,
        publication=False,forecast_posts=0,comment_posts=0,questions=[],timings=events,status='started')
    dest=Path(folder);dest.mkdir(parents=True,exist_ok=True)
    def save():
        (dest/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    rawgit=git;git=TimedGit(git,events);info=git.repository()
    if info.get('private') is not True or info.get('full_name')!=REPO:raise GlobalStop('Private repository required')
    if require_rate and int(rawgit.rate_limit.get('x-ratelimit-remaining') or 0)<3500:
        raise GlobalStop('Insufficient Git API headroom for four unoptimized chains')
    summary['git_rate_at_start']=getattr(rawgit,'rate_limit',{})
    summary[prior_label]=verify_previous(git,clock)
    me=mc.me();listing=mc.http.call(LISTING);ids=[x['id'] for x in listing['results']]
    bindings=[]
    for kind in KINDS:
        post=mc.post(p['posts'][kind])
        if post['question']['type']!=kind:raise GlobalStop('Question type changed')
        b=bind_test_post(post,me,listing_post_ids=ids,now=clock())
        if b['question_contract_sha256']!=p['contracts'][kind]:raise GlobalStop('Frozen question changed')
        bindings.append(b)
    if claude.metadata().get('id')!=OPUS:raise GlobalStop('Model access changed')
    summary['bindings']=bindings;save()
    store=TimedSnapshotStore(git,repository=REPO,branch=state_branch,period=state_period,cap_units=9000000,
        code_sha256=code_sha256,clock=clock,timing_events=events)
    initial=Ledger(dest/'initial.sqlite3',cap_usd='9',period_key=store.period)
    try:store.bootstrap({'journal.sqlite3':database_bytes(initial)})
    finally:initial.close()
    host=DurableHost(store,owner_id(run_id,attempt,secrets.token_hex(16)),dest/'restored',clock=clock,
        credit_expires_utc=p['credit_expires_utc'],key_expires_utc=p['key_expires_utc'],archive_finished=archive_finished)
    started=time.perf_counter()
    try:
        host.ledger.record('frozen-fourtype-input',p)
        host.ledger.record(prior_label.replace('_','-'),summary[prior_label])
        opener=urllib.request.build_opener(SourceNoRedirect())
        def source_open(req,**kwargs):host.before_effect();return opener.open(req,**kwargs)
        def initial_capture(q,urls,searched,d):return capture_selected(q,urls,searched,d,open_request=source_open)
        def child_capture(offered,selected,docs,d):return capture_children(offered,selected,docs,d,open_request=source_open)
        for b in bindings:
            kind=b['contract']['api_type'];event_start=len(events);t=time.perf_counter()
            # Separate source folders while retaining the SAME shared journal and owner.
            host.folder=dest/'restored'/str(b['question_id'])
            result=host.run(b,transport=model_transport(host,claude),count=claude.count,
                capture_initial=capture_initial or initial_capture,capture_follow=capture_follow or child_capture,
                candidates=source_candidates)
            row=dict(kind=kind,post_id=b['post_id'],question_id=b['question_id'],status=result['status'],
                review_decision=result.get('review',{}).get('decision'),review_labels=result.get('review',{}).get('labels',[]),
                seconds=time.perf_counter()-t,stage_timings=result.get('stage_timings',[]),
                diagnostics=result.get('diagnostics',[]),result_sha256=digest(result),
                git_calls=sum(e['kind']=='git-call' for e in events[event_start:]),
                git_seconds=sum(e['seconds'] for e in events[event_start:] if e['kind']=='git-call'),
                snapshot_write_seconds=sum(e['seconds'] for e in events[event_start:] if e['kind']=='snapshot-write'))
            if 'submission_plan' in result:
                row['prepared_payload']=result['submission_plan']['payload']
                row['format_adaptation']=result['submission_plan'].get('format_adaptation')
                row['comment_adaptation']=result['submission_plan'].get('comment_adaptation')
            summary['questions'].append(row);save()
            if result['status']=='global_stop':raise GlobalStop('Stop after uncertain or blocked question')
        summary['status']='completed'
    except BaseException as exc:
        summary.update(status='stopped',error_type=type(exc).__name__,reason=str(exc)[:160] if isinstance(exc,GlobalStop) else 'Host error')
        raise
    finally:
        summary['total_seconds']=time.perf_counter()-started
        summary['actual_cost_units']=host.ledger.db.execute('SELECT COALESCE(SUM(actual),0) FROM operations').fetchone()[0]
        summary['uncertain_reserved_units']=host.ledger.db.execute("SELECT COALESCE(SUM(reserved),0) FROM operations WHERE state IN ('prepared','uncertain')").fetchone()[0]
        summary['settled_calls']=host.ledger.db.execute("SELECT COUNT(*) FROM operations WHERE state='settled'").fetchone()[0]
        try:host.close()
        finally:
            summary.update(state_branch=state_branch,state_head=store.head,owner_released=store.manifest['owner'] is None,
                git_rate_at_end=getattr(rawgit,'rate_limit',{}));save()
            print('FOURTYPE_RESULT '+json.dumps(summary,separators=(',',':'),ensure_ascii=False))
    return summary

def main():
    if os.environ.get('GITHUB_REPOSITORY')!=REPO or os.environ.get('GITHUB_RUN_ATTEMPT')!='1':raise GlobalStop('First run in fixed repository only')
    p=json.loads(Path(os.environ['FROGMAN_INPUT']).read_bytes())
    if digest(p)!=os.environ['FROGMAN_INPUT_SHA']:raise GlobalStop('Frozen input hash changed')
    events=[]
    return run(p,GitHubGit(REPO,os.environ['GITHUB_TOKEN']),ReadOnlyMetaculus(os.environ['METACULUS_TOKEN']),
        ClaudeClient(os.environ['ANTHROPIC_API_KEY'],opener=TimedOpener(events)),
        Path(os.environ['RUNNER_TEMP'])/'fourtype-run',run_id=os.environ['GITHUB_RUN_ID'],attempt=1,
        code_sha256=os.environ['FROGMAN_BUNDLE_SHA'],clock=lambda:dt.datetime.now(dt.timezone.utc),events=events)

if __name__=='__main__':main()
