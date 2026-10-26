"""One question through research, captured text, forecast and host4 review.

No credentials, scheduling or Metaculus writes. A caller-supplied requester
must enforce persistent reservation/usage and save every raw response first.
"""
import copy,json,time
from pathlib import Path
from advisor_policy import OPUS
from question_source_links_v1 import research_payload
from bounded_pipeline import plan_urls
from bounded_evidence import digest,text_answer_check
from independent_runner import Stop
from research_experiment_3 import candidates as source_candidates
from research_assessment_v1 import parse
import candidate_profile_v1 as profile
from source_fallback_v1 import capture_with_alternatives

VERSION='frogman-candidate-pipeline-v8-early-probability-limits'
LIMITS=(12000,8000,5000,3000)

def fit(make,count):
    for limit in LIMITS:
        try:p=make(limit)
        except Stop:continue
        n=count(copy.deepcopy(p))
        if type(n) is not int or n<1:raise Stop('Invalid full token count')
        if n<=30000:return p
    raise Stop('Selected context exceeds input bound')

def run_question(question,folder,*,request,count,clock,save,capture_initial,capture_follow,
                 candidates=source_candidates,guard=lambda:None):
    q=copy.deepcopy(question);r=dict(version=VERSION,question_sha256=digest(q),publication=False,
        status='started',diagnostics=[],assessments=[],raw_answer=None,stage_timings=[])
    def persist():save(copy.deepcopy(r))
    def call(stage,p):
        guard();started=time.perf_counter();raw=None;error=None
        try:
            raw=request(stage,copy.deepcopy(p));guard();return raw
        except BaseException as exc:error=type(exc).__name__;raise
        finally:
            r['stage_timings'].append(dict(stage=stage,seconds=time.perf_counter()-started,
                error_type=error,usage=raw.get('usage') if isinstance(raw,dict) else None,
                stop_reason=raw.get('stop_reason') if isinstance(raw,dict) else None))
    def problem(stage,exc):
        r['diagnostics'].append(dict(stage=stage,error_type=type(exc).__name__,reason=str(exc)[:160] if isinstance(exc,Stop) else 'Validation error'))
    persist()
    try:
        p=research_payload(q,OPUS);p.update(max_tokens=profile.OUT['search'],output_config={'effort':'high'})
        b=json.loads(p['messages'][0]['content']);b['research_time_utc']=clock().isoformat()
        p['messages'][0]['content']=json.dumps(b,ensure_ascii=False)
        raw=call('search',p)
        if raw.get('stop_reason')!='end_turn':raise Stop('Incomplete research plan')
        plan,urls,searched=plan_urls(q,raw)
        if any(len(n)>1000 for n in plan['needs']):raise Stop('Research need length')
        needs=[dict(id='n'+str(i+1),description=n,required=True) for i,n in enumerate(plan['needs'])]
        r.update(plan=plan,needs=needs);persist();guard()
        dest=Path(folder);dest.mkdir(parents=True,exist_ok=True)
        captured=capture_with_alternatives(q,urls,searched,raw,dest,capture=capture_initial,guard=guard)
        docs=copy.deepcopy(captured['documents'])
        r['initial_capture']=captured;r['documents']=docs;persist();guard()
        if not docs:raise Stop('No readable source')
        offered=candidates(q,needs,docs,dest);r['offered_links']=offered
        try:
            p=fit(lambda limit:profile.need_payload(q,docs,clock().isoformat(),needs,
                offered,final=False,limit=limit),count)
            raw=call('need',p);a=parse(raw,p);a['request']=p
            r['assessments'].append(a);persist()
            if a['selected_urls']:
                guard();extra=capture_follow(offered,a['selected_urls'],docs,dest)
                r['child_capture']=extra;docs+=copy.deepcopy(extra['documents']);r['documents']=docs;persist();guard()
        except Stop as exc:
            if getattr(exc,'global_stop',False):raise
            problem('need',exc)
        r['research_frozen_utc']=clock().isoformat();r['documents_sha256']=digest(docs)
        p=fit(lambda limit:profile.payload(q,docs,r['research_frozen_utc'],limit,needs),count)
        # Needs are questions, not evidence. Forecasts receive captured source
        # excerpts, including successful child reads; findings remain audit data.
        r['forecast_request']=p;persist()
        raw=call('forecast',p)
        if raw.get('stop_reason')!='end_turn':raise Stop('Incomplete forecast')
        answer=text_answer_check(raw,q,p)
        r['raw_answer']=answer;persist()
        profile.check_distribution(answer,q)
        if answer['decision']=='abstain':r['status']='abstain';persist();return r
        rp=profile.review_payload(q,answer,profile.audit_documents(p));r['review_request']=rp;persist()
        try:
            guard();review_tokens=count(copy.deepcopy(rp));guard()
            if type(review_tokens) is not int or review_tokens<1:raise Stop('Invalid review token count')
            r['review_preflight_tokens']=review_tokens
            if review_tokens>30000:
                # Known local input limit: no reservation or provider request.
                # Reuse the explicit unavailable policy, never invent a reply.
                r['review_not_requested']='input_limit'
                problem('review',Stop('Review input exceeds token limit; no request sent'))
                rr=None
            else:rr=call('review',rp)
            r['review']=profile.review_check(rr,q,answer,profile.audit_documents(p))
            r['status']='candidate' if r['review']['eligibility']=='allow' else 'rejected'
        except Stop as exc:
            if getattr(exc,'global_stop',False):raise
            problem('review',exc);r['status']='review_error'
    except Stop as exc:
        if getattr(exc,'global_stop',False):r['status']='global_stop'
        else:r['status']='validation_stop'
        problem('pipeline',exc)
    except Exception as exc:r['status']='host_error';problem('pipeline',exc)
    persist();return r
