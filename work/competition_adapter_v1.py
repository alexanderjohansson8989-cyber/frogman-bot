"""Pure Metaculus binding/formatting plus an opt-in injected submission driver.

No HTTP transport or credentials. Default submission is disabled. The driver
is exercised only with simulated transports in this development version.
Sources checked 2026-10-15: official backend common.py, OpenAPI, bot template.
"""
import copy,datetime as dt,html,json,math,hashlib
from autonomous_runner import distribution_check
from bounded_evidence import digest
from bounded_pipeline import audit_documents
from general_evaluation import instant
from general_scoring import loginterval
from numeric_cdf import validate_numeric_cdf
from mc_option_contract_v1 import option_contract,expected_values
from cdf_projection_v1 import raw_mixture_cdf,project_cdf
from numeric_diagnostics_v1 import projection_diagnostics
from independent_runner import Stop
from operations_ledger_v1 import GlobalStop,readback_scope

VERSION='competition-adapter-v4-bounded-source-comments'
TYPES=('binary','multiple_choice','numeric','discrete')

def positive_id(n):
    if type(n) is not int or n<=0:raise Stop('Positive numeric identity required')
    return n

def identity(me,expected_username):
    if me.get('username')!=expected_username:raise Stop('Unexpected acting account')
    return dict(id=positive_id(me['id']),username=me['username'])

def timeline(q):
    """One representation for question hashes, dispatch and readback."""
    out={}
    for k in ('open_time','scheduled_close_time','actual_close_time',
              'spot_scoring_time','scheduled_resolve_time','actual_resolve_time'):
        v=q.get(k)
        try:out[k]=None if v is None else instant(v).isoformat()
        except (ValueError,TypeError,OverflowError) as exc:raise Stop('Invalid question time: '+k) from exc
    if out['open_time'] is None:raise Stop('Opening time required')
    if out['scheduled_close_time'] is None:raise Stop('Scheduled closing time required')
    if out['actual_close_time'] is None:
        values=[instant(out['scheduled_close_time'])]
        if out['actual_resolve_time'] is not None:values.append(instant(out['actual_resolve_time']))
        out['actual_close_time']=min(values).isoformat()
    return out

def question_data(post):
    if not isinstance(post.get('question'),dict) or post.get('group_of_questions') or post.get('conditional'):
        raise Stop('Only standalone questions supported in version 1')
    q=post['question'];positive_id(post['id']);positive_id(q['id'])
    if q['type'] not in TYPES:raise Stop('Unsupported question type')
    for k in ('title','description','resolution_criteria','fine_print'):
        if k not in q or (q[k] is not None and not isinstance(q[k],str)):raise Stop('Full question content required')
    if not q['title'] or not q['title'].strip() or not q['resolution_criteria'] or not q['resolution_criteria'].strip():raise Stop('Masked or incomplete question')
    out=dict(id=str(q['id']),type='categorical' if q['type']=='multiple_choice' else 'numeric' if q['type']=='discrete' else q['type'],
        question=q['title'],description=q['description'],fine_print=q['fine_print'],
        criteria=q['resolution_criteria']+'\nFine print:\n'+(q['fine_print'] or ''),
        resolution_source=f"https://www.metaculus.com/questions/{post['id']}/")
    if q['type'] in ('binary','multiple_choice'):
        out['submission_constraints']=dict(minimum_probability=.001,maximum_probability=.999,no_automatic_clipping=True)
    out['api_type']=q['type']
    out['timeline']=timeline(q)
    if q['type']=='multiple_choice':
        try:mc=option_contract(q)
        except ValueError as exc:raise Stop(str(exc)) from exc
        out['choices']=mc['active']
        out['option_order_origin']=mc['origin']
    elif q['type'] in ('numeric','discrete'):
        s=q.get('scaling')
        if (not isinstance(s,dict) or not {'continuous_range','range_min','range_max'}<=set(s)
                or not {'inbound_outcome_count','open_lower_bound','open_upper_bound'}<=set(q)):
            raise Stop('Explicit numeric scaling/bounds required')
        grid=s['continuous_range'];count=q['inbound_outcome_count']
        if (type(count) is not int or not 1<=count<=2000 or not isinstance(grid,list) or len(grid)!=count+1
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in grid) or any(a>=b for a,b in zip(grid,grid[1:]))
            or type(q['open_lower_bound']) is not bool or type(q['open_upper_bound']) is not bool
            or grid[0]!=s['range_min'] or grid[-1]!=s['range_max']):raise Stop('Explicit numeric grid/bounds required')
        for k in ('open_lower_bound','open_upper_bound','inbound_outcome_count'):
            if s.get(k)!=q[k]:raise Stop('Conflicting numeric grid metadata')
        out.update(unit=q.get('unit'),lower_bound=None if q['open_lower_bound'] else grid[0],
                   upper_bound=None if q['open_upper_bound'] else grid[-1])
        out['submission_constraints']=dict(continuous_range=copy.deepcopy(grid),inbound_outcome_count=count,
            minimum_bucket_mass=round(.01/count,9),maximum_bucket_mass=.2*200/count,
            minimum_open_tail=.001,adaptation='bounded_simplex_l2_with_logged_probability_changes')
        if q['type']=='discrete':
            centers=[(a+b)/2 for a,b in zip(grid,grid[1:])]
            if (not isinstance(s.get('nominal_min'),(int,float)) or not isinstance(s.get('nominal_max'),(int,float))
                or not math.isclose(centers[0],s['nominal_min'],abs_tol=1e-9)
                or not math.isclose(centers[-1],s['nominal_max'],abs_tol=1e-9)):
                raise Stop('Discrete centers conflict with API nominal bounds')
            out['discrete_outcomes']=dict(centers=centers,
                interpretation='The mixture is a latent distribution. Probabilities are its mass between the supplied grid edges, assigned to discrete centers; open tails remain separate.')
    return out

def bind_post(post,me,*,tournament_id,expected_username,now):
    """PURE validation of fetched JSON. No HTTP, storage, forecast or comment write."""
    actor=identity(me,expected_username);positive_id(tournament_id);question=question_data(post);q=post['question']
    if not any(p.get('id')==tournament_id for p in post.get('projects',{}).get('tournament',[])):
        raise Stop('Tournament membership not verified')
    if post.get('status')!='open' or q.get('status')!='open' or q.get('resolution') is not None:
        raise Stop('Question is not open')
    if instant(question['timeline']['open_time'])>now:raise Stop('Question has not opened')
    # A missing spot time is not substituted with the scheduled closing time.
    if question['timeline']['spot_scoring_time'] is None:raise Stop('Spot scoring time required')
    times=[instant(question['timeline'][k]) for k in ('scheduled_close_time','actual_close_time','spot_scoring_time')]
    cutoff=min(times)
    if now>=cutoff:raise Stop('Closing or spot scoring time passed')
    if 'my_forecasts' not in q or not isinstance(q['my_forecasts'],dict) or 'latest' not in q['my_forecasts']:
        raise Stop('Authenticated previous-forecast state missing')
    if q['my_forecasts']['latest'] is not None:raise Stop('A forecast already exists')
    contract=dict(question=question,api_type=q['type'],scaling=q.get('scaling'),
        options_history=q.get('options_history'),all_options_ever=q.get('all_options_ever'))
    return dict(version=VERSION,post_id=post['id'],question_id=q['id'],tournament_id=tournament_id,
        actor=actor,question=question,question_contract_sha256=digest(contract),
        contract=contract,cutoff_utc=cutoff.isoformat(),bound_at_utc=now.isoformat())

def bind_test_post(post,me,*,listing_post_ids,now):
    """Explicit non-submittable test-area path after an authenticated filtered GET.

    Test questions can remain open long after spot scoring. Keep the real spot
    time in the question; use only close time for this diagnostic execution.
    Production bind_post retains its membership and spot-deadline checks.
    """
    actor=identity(me,'Frogman-bot');question=question_data(post);q=post['question']
    if post['id'] not in listing_post_ids:raise Stop('Test-area listing membership missing')
    if post.get('status')!='open' or q.get('status')!='open' or q.get('resolution') is not None:
        raise Stop('Test question is not open')
    cutoff=min(instant(question['timeline'][k]) for k in ('scheduled_close_time','actual_close_time'))
    if not instant(question['timeline']['open_time'])<=now<cutoff:raise Stop('Test question outside opening window')
    if not isinstance(q.get('my_forecasts'),dict) or q['my_forecasts'].get('latest','missing') is not None:
        raise Stop('Missing or existing previous forecast')
    contract=dict(question=question,api_type=q['type'],scaling=q.get('scaling'),
        options_history=q.get('options_history'),all_options_ever=q.get('all_options_ever'))
    return dict(version=VERSION,mode='test_area_diagnostic',post_id=post['id'],question_id=q['id'],
        tournament_id='bot-testing-area',actor=actor,question=question,contract=contract,
        question_contract_sha256=digest(contract),cutoff_utc=cutoff.isoformat(),bound_at_utc=now.isoformat(),
        actual_spot_scoring_time=q.get('spot_scoring_time'),submission_enabled=False)


def numeric_adaptation(d,q):
    result=project_cdf(raw_mixture_cdf(d,q['scaling']['continuous_range']),q)
    result['diagnostics']=projection_diagnostics(result,q)
    return result

def numeric_payload(d,q):
    return numeric_adaptation(d,q)['continuous_cdf']


def short_comment_rationale(value,*,escaped_limit=1500):
    """Bound only the separate comment, preserving the original forecast.

    An explicitly labelled prefix is an excerpt, never an invented summary.
    Prefer a complete sentence end within the limit, otherwise a word boundary.
    """
    if not isinstance(value,str) or not 10<=len(value.strip())<=4000:
        raise Stop('Bounded question-specific rationale required')
    if type(escaped_limit) is not int or escaped_limit<30:raise Stop('Insufficient comment rationale space')
    text=value.strip()
    if len(text)<=650 and len(html.escape(text))<=escaped_limit:return text,None
    import re
    prefix='Rationale excerpt: ';limit=650-len(prefix)-1
    # Select a raw prefix; never slice an already escaped HTML entity.
    limit=min(limit,len(text))
    while limit>0 and len(html.escape(prefix+text[:limit].rstrip()+'…'))>escaped_limit:limit-=1
    if limit<10:raise Stop('Insufficient comment rationale space')
    ends=[m.end() for m in re.finditer(r'[.!?](?=\s|$)',text[:limit+1]) if m.end()<=limit]
    cut=ends[-1] if ends else text.rfind(' ',0,limit+1)
    if cut<10:cut=limit
    excerpt=prefix+text[:cut].rstrip()+'…'
    return excerpt,dict(method='labelled-prefix-excerpt-v1',original_sha256=digest(value),
        original_characters=len(value),comment_characters=len(excerpt))

def prepare(binding,result):
    q=binding['question'];a=result['raw_answer'];review=result.get('review',{});adaptation=None
    from classified_review_v1 import eligible
    if (result.get('status')!='candidate' or result['question_sha256']!=digest(q)
        or review.get('question_sha256')!=digest(q)
        or not eligible(review,q,a,audit_documents(result['forecast_request'])) or review.get('original_answer_sha256')!=digest(a)
        or review.get('usable_answer')!=a or review.get('documents_sha256')!=digest(audit_documents(result['forecast_request']))
        or json.loads(result['forecast_request']['messages'][0]['content'])['question']!=q):raise Stop('Reviewed frozen candidate required')
    distribution_check(a['distribution'],q);d=a['distribution'];field={}
    if q['type']=='binary':
        if not .001<=d['probability']<=.999:raise Stop('Binary probability outside API limits')
        field={'probability_yes':d['probability']};expected=[1-d['probability'],d['probability']]
    elif q['type']=='categorical':
        p=d['probabilities']
        if any(not .001<=v<=.999 for v in p.values()):raise Stop('Category probability outside API limits')
        field={'probability_yes_per_category':copy.deepcopy(p)}
        expected=expected_values(p,option_contract(dict(options=q['choices'],
            all_options_ever=binding['contract']['all_options_ever'],options_history=binding['contract']['options_history'])))
    else:
        fields=dict(binding['contract']['scaling']);fields={'scaling':fields,**{k:fields[k] for k in ('inbound_outcome_count','open_lower_bound','open_upper_bound')}}
        adaptation=numeric_adaptation(d,fields)
        field={'continuous_cdf':adaptation['continuous_cdf']};expected=field['continuous_cdf']
    urls=list(dict.fromkeys(f['url'] for f in a['facts']))
    if any(not isinstance(u,str) or not u.startswith('https://') or len(u)>2048 for u in urls):raise Stop('Bounded cited source URLs required')
    # Only comment presentation changes. Never shorten a URL into a different link.
    displayed=[];omitted=[]
    for u in urls:
        escaped=html.escape(u)
        if len(escaped)<=300:displayed.append(escaped)
        else:
            url_sha256=hashlib.sha256(u.encode('utf-8')).hexdigest()
            omitted.append(dict(url_sha256=url_sha256,reason='comment_source_length'))
            displayed.append('[Source URL omitted for length; SHA256 '+url_sha256+']')
    sources='\n\nSources:\n'+'\n'.join(displayed)
    rationale,comment_adaptation=short_comment_rationale(a['rationale'],escaped_limit=1500-len(sources))
    if omitted:comment_adaptation=dict(comment_adaptation or {},source_url_omissions=omitted)
    comment=html.escape(rationale)+sources
    if len(comment)>1500:raise Stop('Comment too long')
    plan=dict(version=VERSION,binding=copy.deepcopy(binding),answer_sha256=digest(a),
        payload=[dict(question=binding['question_id'],source='api',**field)],expected_forecast_values=expected,
        comment=dict(on_post=binding['post_id'],text=comment,is_private=True,included_forecast=True),
        submission_enabled=False,format_adaptation=adaptation,comment_adaptation=comment_adaptation,
        review_policy=review.get('version','legacy'),review_decision=review['decision'],
        review_labels=copy.deepcopy(review.get('labels',[])))
    return dict(**plan,plan_sha256=digest(plan))

def check_plan(plan):
    body={k:v for k,v in plan.items() if k!='plan_sha256'}
    if digest(body)!=plan.get('plan_sha256') or plan.get('version')!=VERSION:raise Stop('Frozen submission plan changed')

def verify_readback(plan,post,me,*,lifecycle_at=None):
    check_plan(plan)
    b=plan['binding'];actor=identity(me,b['actor']['username'])
    if actor!=b['actor'] or post.get('id')!=b['post_id'] or post['question'].get('id')!=b['question_id']:raise Stop('Readback identity mismatch')
    if not any(p.get('id')==b['tournament_id'] for p in post.get('projects',{}).get('tournament',[])):raise Stop('Readback tournament mismatch')
    q=post['question'];contract=dict(question=question_data(post),api_type=q['type'],scaling=q.get('scaling'),
        options_history=q.get('options_history'),all_options_ever=q.get('all_options_ever'))
    changed=digest(contract)!=b['question_contract_sha256']
    if changed:
        # Recovery may observe a normal closure/resolution after receipt. Only
        # these two actual timestamps may differ; criteria, schedules, options
        # and scaling still have to match the original contract byte-for-byte.
        if lifecycle_at is None or post.get('status') not in ('closed','resolved') or q.get('status') not in ('closed','resolved'):
            raise Stop('Question changed during submission')
        original=b['contract']['question']['timeline'];current=contract['question']['timeline']
        normalized=copy.deepcopy(contract)
        latest=q.get('my_forecasts',{}).get('latest') or {}
        start=latest.get('start_time')
        if type(start) not in (int,float) or not math.isfinite(start):raise Stop('Readback forecast timestamp missing')
        for field in ('actual_close_time','actual_resolve_time'):
            if current[field]!=original[field]:
                if current[field] is None:raise Stop('Lifecycle time disappeared')
                if field=='actual_resolve_time' and original[field] is not None:
                    raise Stop('Previously recorded resolution time changed')
                t=instant(current[field])
                if not start<t.timestamp()<=lifecycle_at.timestamp():raise Stop('Lifecycle change precedes forecast or is in future')
                normalized['question']['timeline'][field]=original[field]
        if digest(normalized)!=b['question_contract_sha256']:
            raise Stop('Substantive question contract changed')
    times=contract['question']['timeline']
    if times['spot_scoring_time'] is None:raise Stop('Spot scoring time required')
    current_cutoff=min(instant(times[k]) for k in ('scheduled_close_time','actual_close_time','spot_scoring_time'))
    if current_cutoff!=instant(b['cutoff_utc']) and not changed:raise Stop('Submission deadline changed during readback')
    latest=q['my_forecasts']['latest']
    if not latest or latest.get('author_id')!=actor['id'] or latest.get('question_id')!=b['question_id']:raise Stop('Forecast author/question mismatch')
    start=latest.get('start_time')
    if type(start) not in (int,float) or not math.isfinite(start):raise Stop('Readback forecast timestamp missing')
    if not instant(b['bound_at_utc']).timestamp()-1<=start<min(instant(b['cutoff_utc']),current_cutoff).timestamp():raise Stop('Stale or late forecast readback')
    if latest.get('end_time') is not None:raise Stop('Forecast has an unexpected withdrawal time')
    got=latest['forecast_values'];expected=plan['expected_forecast_values']
    if not isinstance(got,list) or len(got)!=len(expected):raise Stop('Readback distribution length')
    for actual,wanted in zip(got,expected):
        if wanted is None:
            if actual is not None:raise Stop('Inactive option gained probability')
        elif type(actual) not in (int,float) or not math.isfinite(actual) or abs(actual-wanted)>1e-9:raise Stop('Readback distribution differs')
    return dict(post_id=b['post_id'],question_id=b['question_id'],author_id=actor['id'],
                forecast_start_time=start,values_sha256=digest(got),verified=True)

def submit_once(plan,ledger,*,fetch,post_forecast,clock,enabled=False):
    if enabled is not True:raise Stop('Submission is disabled')
    check_plan(plan)
    if plan['binding'].get('mode')=='test_area_diagnostic':raise Stop('Diagnostic plans cannot be submitted')
    b=plan['binding'];me,post=fetch(b['post_id'])
    fresh=bind_post(post,me,tournament_id=b['tournament_id'],expected_username=b['actor']['username'],now=clock())
    if fresh['question_contract_sha256']!=b['question_contract_sha256'] or fresh['actor']!=b['actor'] or fresh['cutoff_utc']!=b['cutoff_utc']:raise Stop('Submission contract changed')
    key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    ledger.prepare_submission(key,plan)
    attest=getattr(post_forecast,'checkpoint_dispatch_contract',None)
    if attest is not None:attest(ledger,key,plan)
    if clock()>=instant(b['cutoff_utc']):raise Stop('Submission deadline passed')
    ledger.begin_submission(key,plan)
    if clock()>=instant(b['cutoff_utc']):raise Stop('Submission deadline passed')
    from candidate_api_v1 import TransportOutcomeUnknown
    try:post_forecast(copy.deepcopy(plan['payload']))
    except TransportOutcomeUnknown:pass
    except GlobalStop:raise
    except Exception:pass  # An unclassified injected transport may have sent it.
    guard=getattr(post_forecast,'before_readback',None)
    return reconcile(plan,ledger,fetch=fetch,before_readback=guard)

def reconcile(plan,ledger,*,fetch,before_readback=None,lifecycle_at=None):
    b=plan['binding']
    key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    with readback_scope(ledger,key):
        return _reconcile(plan,ledger,fetch=fetch,before_readback=before_readback,lifecycle_at=lifecycle_at)

def _reconcile(plan,ledger,*,fetch,before_readback=None,lifecycle_at=None):
    if before_readback:before_readback()
    b=plan['binding'];me,post=fetch(b['post_id'])
    if before_readback:before_readback()
    receipt=verify_readback(plan,post,me,lifecycle_at=lifecycle_at)
    key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    ledger.confirm_submission(key,plan,receipt)
    return receipt
