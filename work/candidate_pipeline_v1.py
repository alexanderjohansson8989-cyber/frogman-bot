"""One question through research, captured text, forecast and host4 review.

No credentials, scheduling or Metaculus writes. A caller-supplied requester
must enforce persistent reservation/usage and save every raw response first.
"""
import copy,json
from pathlib import Path
from advisor_policy import OPUS
from autonomous_runner import research_payload
from bounded_pipeline import plan_urls
from bounded_evidence import digest,text_answer_check
from independent_runner import Stop
from research_experiment_3 import candidates as source_candidates
from research_assessment_v1 import parse
import candidate_profile_v1 as profile

VERSION='frogman-candidate-pipeline-v1'
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
        status='started',diagnostics=[],assessments=[],raw_answer=None)
    def persist():save(copy.deepcopy(r))
    def call(stage,p):
        guard();raw=request(stage,copy.deepcopy(p));guard();return raw
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
        captured=capture_initial(q,urls,searched,dest);docs=copy.deepcopy(captured['documents'])
        r['initial_capture']=captured;r['documents']=docs;persist();guard()
        if not docs:raise Stop('No readable source')
        offered=candidates(q,needs,docs,dest);r['offered_links']=offered
        for turn in range(2):
            try:
                p=fit(lambda limit:profile.need_payload(q,docs,clock().isoformat(),needs,
                    offered if not turn else [],final=bool(turn),limit=limit),count)
                raw=call('need',p);a=parse(raw,p);a['request']=p
                r['assessments'].append(a);persist()
                if turn or not a['selected_urls']:break
                guard();extra=capture_follow(offered,a['selected_urls'],docs,dest)
                r['child_capture']=extra;docs+=copy.deepcopy(extra['documents']);r['documents']=docs;persist();guard()
            except Stop as exc:
                # Budget/transport halts must propagate; requester marks them.
                if getattr(exc,'global_stop',False):raise
                problem('need',exc);break
        r['research_frozen_utc']=clock().isoformat();r['documents_sha256']=digest(docs)
        p=fit(lambda limit:profile.payload(q,docs,r['research_frozen_utc'],limit,needs),count)
        # Needs describe questions only. The assessment ledger supplies sent,
        # positioned evidence; unverified research-plan hints are not facts.
        r['forecast_request']=p;persist()
        raw=call('forecast',p)
        if raw.get('stop_reason')!='end_turn':raise Stop('Incomplete forecast')
        answer=text_answer_check(raw,q,p)
        r['raw_answer']=answer;persist()
        profile.check_distribution(answer)
        if answer['decision']=='abstain':r['status']='abstain';persist();return r
        rp=profile.review_payload(q,answer,profile.audit_documents(p));r['review_request']=rp;persist()
        try:
            rr=call('review',rp);r['review']=profile.review_check(rr,q,answer,profile.audit_documents(p))
            r['status']='candidate' if r['review']['decision']=='pass' else 'rejected'
        except Stop as exc:
            if getattr(exc,'global_stop',False):raise
            problem('review',exc);r['status']='review_error'
    except Stop as exc:
        if getattr(exc,'global_stop',False):r['status']='global_stop'
        else:r['status']='validation_stop'
        problem('pipeline',exc)
    except Exception as exc:r['status']='host_error';problem('pipeline',exc)
    persist();return r
