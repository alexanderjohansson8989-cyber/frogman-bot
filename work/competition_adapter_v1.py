"""Pure Metaculus binding/formatting plus an opt-in injected submission driver.

No HTTP transport or credentials. Default submission is disabled. The driver
is exercised only with simulated transports in this development version.
Sources checked 2026-10-15: official backend common.py, OpenAPI, bot template.
"""
import copy,datetime as dt,html,json,math
from autonomous_runner import distribution_check
from bounded_evidence import digest
from bounded_pipeline import audit_documents
from general_evaluation import instant
from general_scoring import loginterval
from numeric_cdf import validate_numeric_cdf
from independent_runner import Stop
from operations_ledger_v1 import GlobalStop

VERSION='competition-adapter-v1'
TYPES=('binary','multiple_choice','numeric')

def positive_id(n):
    if type(n) is not int or n<=0:raise Stop('Positive numeric identity required')
    return n

def identity(me,expected_username):
    if me.get('username')!=expected_username:raise Stop('Unexpected acting account')
    return dict(id=positive_id(me['id']),username=me['username'])

def question_data(post):
    if not isinstance(post.get('question'),dict) or post.get('group_of_questions') or post.get('conditional'):
        raise Stop('Only standalone questions supported in version 1')
    q=post['question'];positive_id(post['id']);positive_id(q['id'])
    if q['type'] not in TYPES:raise Stop('Unsupported question type')
    for k in ('title','description','resolution_criteria','fine_print'):
        if k not in q or (q[k] is not None and not isinstance(q[k],str)):raise Stop('Full question content required')
    if not q['title'] or not q['title'].strip() or not q['resolution_criteria'] or not q['resolution_criteria'].strip():raise Stop('Masked or incomplete question')
    out=dict(id=str(q['id']),type='categorical' if q['type']=='multiple_choice' else q['type'],
        question=q['title'],description=q['description'],fine_print=q['fine_print'],
        criteria=q['resolution_criteria']+'\nFine print:\n'+(q['fine_print'] or ''),
        resolution_source=f"https://www.metaculus.com/questions/{post['id']}/")
    if q['type'] in ('binary','multiple_choice'):
        out['submission_constraints']=dict(minimum_probability=.001,maximum_probability=.999,no_automatic_clipping=True)
    if q['type']=='multiple_choice':
        options=q['options'];ever=q['all_options_ever']
        if (not isinstance(options,list) or len(options)<2 or any(not isinstance(s,str) or not s for s in options)
            or len(set(options))!=len(options) or not isinstance(ever,list)
            or any(not isinstance(s,str) or not s for s in ever) or len(set(ever))!=len(ever) or not set(options)<=set(ever)):
            raise Stop('Explicit active and historical options required')
        out['choices']=copy.deepcopy(options)
    elif q['type']=='numeric':
        s=q['scaling'];grid=s['continuous_range'];count=q['inbound_outcome_count']
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
            minimum_open_tail=.001,no_automatic_smoothing=True)
    return out

def bind_post(post,me,*,tournament_id,expected_username,now):
    """PURE validation of fetched JSON. No HTTP, storage, forecast or comment write."""
    actor=identity(me,expected_username);positive_id(tournament_id);question=question_data(post);q=post['question']
    if not any(p.get('id')==tournament_id for p in post.get('projects',{}).get('tournament',[])):
        raise Stop('Tournament membership not verified')
    if post.get('status')!='open' or q.get('status')!='open' or q.get('resolution') is not None:
        raise Stop('Question is not open')
    if instant(q['open_time'])>now:raise Stop('Question has not opened')
    # A missing spot time is not substituted with the scheduled closing time.
    times=[instant(q['scheduled_close_time']),instant(q['actual_close_time']),instant(q['spot_scoring_time'])]
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

def numeric_payload(d,q):
    lo=-math.inf if d['lower_bound'] is None else d['lower_bound'];hi=math.inf if d['upper_bound'] is None else d['upper_bound']
    total=math.fsum(c['weight'] for c in d['components']);cdf=[]
    for x in q['scaling']['continuous_range']:
        if x<=lo:value=0.
        elif x>=hi:value=1.
        else:
            value=math.fsum(c['weight']/total*math.exp(loginterval((lo-c['mean'])/c['sd'],(x-c['mean'])/c['sd'])-
                loginterval((lo-c['mean'])/c['sd'],(hi-c['mean'])/c['sd'])) for c in d['components'] if c['weight'])
        cdf.append(value)
    # No smoothing, floor, clipping or widening to pass API constraints.
    return validate_numeric_cdf(cdf,q)

def prepare(binding,result):
    q=binding['question'];a=result['raw_answer'];review=result.get('review',{})
    if (result.get('status')!='candidate' or result['question_sha256']!=digest(q)
        or review.get('question_sha256')!=digest(q)
        or review.get('decision')!='pass' or review.get('original_answer_sha256')!=digest(a)
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
        expected=[p.get(k) for k in binding['contract']['all_options_ever']]
    else:
        fields=dict(binding['contract']['scaling']);fields={'scaling':fields,**{k:fields[k] for k in ('inbound_outcome_count','open_lower_bound','open_upper_bound')}}
        field={'continuous_cdf':numeric_payload(d,fields)};expected=field['continuous_cdf']
    rationale=a['rationale'].strip()
    if not 10<=len(rationale)<=650:raise Stop('Short question-specific rationale required')
    urls=list(dict.fromkeys(f['url'] for f in a['facts']))
    if any(not isinstance(u,str) or not u.startswith('https://') or len(u)>300 for u in urls):raise Stop('Bounded cited source URLs required')
    comment=html.escape(rationale)+'\n\nSources:\n'+'\n'.join(urls)
    if len(comment)>1500:raise Stop('Comment too long')
    plan=dict(version=VERSION,binding=copy.deepcopy(binding),answer_sha256=digest(a),
        payload=[dict(question=binding['question_id'],source='api',**field)],expected_forecast_values=expected,
        comment=dict(on_post=binding['post_id'],text=comment,is_private=True,included_forecast=True),
        submission_enabled=False)
    return dict(**plan,plan_sha256=digest(plan))

def check_plan(plan):
    body={k:v for k,v in plan.items() if k!='plan_sha256'}
    if digest(body)!=plan.get('plan_sha256') or plan.get('version')!=VERSION:raise Stop('Frozen submission plan changed')

def verify_readback(plan,post,me):
    check_plan(plan)
    b=plan['binding'];actor=identity(me,b['actor']['username'])
    if actor!=b['actor'] or post.get('id')!=b['post_id'] or post['question'].get('id')!=b['question_id']:raise Stop('Readback identity mismatch')
    if not any(p.get('id')==b['tournament_id'] for p in post.get('projects',{}).get('tournament',[])):raise Stop('Readback tournament mismatch')
    q=post['question'];contract=dict(question=question_data(post),api_type=q['type'],scaling=q.get('scaling'),
        options_history=q.get('options_history'),all_options_ever=q.get('all_options_ever'))
    if digest(contract)!=b['question_contract_sha256']:raise Stop('Question changed during submission')
    current_cutoff=min(instant(q[k]) for k in ('scheduled_close_time','actual_close_time','spot_scoring_time'))
    if current_cutoff!=instant(b['cutoff_utc']):raise Stop('Submission deadline changed during readback')
    latest=q['my_forecasts']['latest']
    if not latest or latest.get('author_id')!=actor['id'] or latest.get('question_id')!=b['question_id']:raise Stop('Forecast author/question mismatch')
    start=latest.get('start_time')
    if type(start) not in (int,float) or not math.isfinite(start):raise Stop('Readback forecast timestamp missing')
    if not instant(b['bound_at_utc']).timestamp()-1<=start<instant(b['cutoff_utc']).timestamp():raise Stop('Stale or late forecast readback')
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
    b=plan['binding'];me,post=fetch(b['post_id'])
    fresh=bind_post(post,me,tournament_id=b['tournament_id'],expected_username=b['actor']['username'],now=clock())
    if fresh['question_contract_sha256']!=b['question_contract_sha256'] or fresh['actor']!=b['actor'] or fresh['cutoff_utc']!=b['cutoff_utc']:raise Stop('Submission contract changed')
    key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    ledger.prepare_submission(key,plan)
    if clock()>=instant(b['cutoff_utc']):raise Stop('Submission deadline passed')
    ledger.begin_submission(key,plan)
    if clock()>=instant(b['cutoff_utc']):raise Stop('Submission deadline passed')
    try:post_forecast(copy.deepcopy(plan['payload']))
    except Exception:pass  # It may have reached the server; always read back.
    return reconcile(plan,ledger,fetch=fetch)

def reconcile(plan,ledger,*,fetch):
    b=plan['binding'];me,post=fetch(b['post_id'])
    receipt=verify_readback(plan,post,me)
    key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    ledger.confirm_submission(key,plan,receipt)
    return receipt
