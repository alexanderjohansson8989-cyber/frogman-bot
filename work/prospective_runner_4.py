"""Versioned High whole-chain comparison. Transport/capture/save are injected.

No submission, credentials, scheduling, model retries or changes to old runners.
"""
import copy
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from advisor_policy import OPUS,FABLE,RATES
from autonomous_runner import research_payload
from bounded_evidence import digest,text_answer_check
from bounded_pipeline import plan_urls
from general_evaluation import instant
from independent_runner import Stop
from prospective_design_4 import validate
from research_enrichment import assess
from research_experiment_3 import candidates
import research_profile_2 as profile

VERSION='prospective-runner-4'
PAIR_RESERVE=Decimal('7.840')
OUT={'search':6000,'need':6000,'forecast':12000,'review':4000}
SLOTS={'search':1,'need':2,'forecast':1,'review':1}
LIMITS=(12000,8000,5000,3000)

class Halt(Stop):
    def __init__(self,status):super().__init__(status);self.status=status

def public_question(q):
    keys=('id','type','question','criteria','resolution_source','outcome_not_before_utc',
          'resolution','scoring','choices','lower_bound','upper_bound')
    return {k:copy.deepcopy(q[k]) for k in keys if k in q}

def stage_cap(arm,kind):
    if kind=='search':return Decimal('1.20') if arm=='opus' else Decimal('3.00')
    model=OPUS if kind=='review' or arm=='opus' else FABLE
    a,b=RATES[model]
    return (Decimal(36000)*a+Decimal(OUT[kind])*b)/1000000

def cost(reply,model,kind):
    if reply.get('model')!=model:raise Stop('Unexpected model')
    u=reply.get('usage',{});n=u.get('input_tokens');out=u.get('output_tokens')
    if type(n) is not int or not 0<n<=200000 or type(out) is not int or not 0<=out<=OUT[kind]:raise Stop('Unknown token accounting')
    if any(u.get(k,0)!=0 for k in ('cache_read_input_tokens','cache_creation_input_tokens')):raise Stop('Cache not budgeted')
    if u.get('service_tier','standard')!='standard' or u.get('inference_geo','global')!='global':raise Stop('Unexpected tier/geography')
    tools=u.get('server_tool_use') or {};searches=tools.get('web_search_requests',0)
    if type(searches) is not int or not 0<=searches<=2 or any(v for k,v in tools.items() if k!='web_search_requests') or (kind!='search' and searches):raise Stop('Unbudgeted server tool')
    if u.get('iterations'):raise Stop('Unexpected iterative/advisor accounting')
    a,b=RATES[model]
    return (Decimal(n)*a+Decimal(out)*b)/1000000+Decimal(searches)*Decimal('.01')

class Journal:
    def __init__(self,r,call,clock,window,save,budget):
        self.r=r;self.call=call;self.clock=clock;self.window=window;self.save=save;self.budget=budget;self.pair=None
    def persist(self):self.save(copy.deepcopy(self.r))
    def guard(self):
        t=self.clock()
        if not instant(self.window['opens_utc'])<=t<instant(self.window['closes_utc']):raise Halt('time_stop')
        if self.pair is not None and t>=instant(self.pair['started_utc'])+dt.timedelta(hours=1):
            self.pair['status']='pair_time_error';raise Halt('pair_time_stop')
    def begin_pair(self,q,index):
        self.guard()
        if self.pair is not None or Decimal(self.r['uncertain_reserved_usd'])!=0:raise Halt('reservation_conflict')
        if Decimal(self.r['spent_estimate_usd'])+PAIR_RESERVE>self.budget:raise Halt('budget_stop')
        pair=dict(question_id=q['id'],started_utc=self.clock().isoformat(),reservation_usd=str(PAIR_RESERVE),
            unspent_reserved_usd=str(PAIR_RESERVE),status='running',order=['opus','fable'] if index%2==0 else ['fable','opus'],arms={})
        self.r['pairs'].append(pair);self.pair=pair;self.persist();return pair
    def finish_pair(self):
        self.guard();self.pair.update(status='complete',completed_utc=self.clock().isoformat(),unspent_reserved_usd='0')
        self.pair=None;self.persist()
    def count(self,p):
        self.guard();v=self.call('messages/count_tokens',{k:copy.deepcopy(v) for k,v in p.items() if k not in ('max_tokens','output_config')})
        self.guard();n=v.get('input_tokens')
        if type(n) is not int or n<1:raise Stop('Invalid full token count')
        return n
    def request(self,arm,kind,p):
        self.guard()
        if self.pair is None or arm not in ('opus','fable') or kind not in OUT:raise Stop('No reserved stage')
        model=OPUS if kind=='review' or arm=='opus' else FABLE
        effort='medium' if kind=='review' else 'high'
        p=copy.deepcopy(p)
        expected={'model','system','messages','output_config','max_tokens'}|({'tools'} if kind=='search' else set())
        if (set(p)!=expected or p['model']!=model or p['output_config']!={'effort':effort}
            or type(p['max_tokens']) is not int or p['max_tokens']!=OUT[kind]
            or not isinstance(p['system'],str) or not isinstance(p['messages'],list) or not p['messages']
            or any(set(m)!={'role','content'} or m['role']!='user' or not isinstance(m['content'],str) for m in p['messages'])):
            raise Stop('Frozen stage request contract')
        if kind=='search' and p['tools']!=[dict(type='web_search_20250305',name='web_search',max_uses=2)]:raise Stop('Search contract')
        prior=[s for s in self.r['stages'] if s['question_id']==self.pair['question_id'] and s['arm']==arm and s['kind']==kind]
        if len(prior)>=SLOTS[kind] or len(self.r['stages'])>=100:raise Stop('Generation slot exhausted')
        n=self.count(p)
        if n>30000:raise Stop('Full input over30000')
        reserved_input=(n*120+99)//100;a,b=RATES[model];cap=stage_cap(arm,kind)
        reserve=cap if kind=='search' else (Decimal(reserved_input)*a+Decimal(OUT[kind])*b)/1000000
        remaining=Decimal(self.pair['unspent_reserved_usd'])
        if reserve>cap or cap>remaining or Decimal(self.r['spent_estimate_usd'])+remaining>self.budget:raise Halt('budget_stop')
        stage=dict(name=self.pair['question_id']+'/'+arm+'/'+kind,question_id=self.pair['question_id'],arm=arm,kind=kind,
            payload=p,payload_sha256=digest(p),preflight_input=n,reserved_input=reserved_input,
            reserve_usd=str(reserve),slot_cap_usd=str(cap),started_utc=self.clock().isoformat())
        self.r['stages'].append(stage);self.pair['unspent_reserved_usd']=str(remaining-cap)
        self.r['uncertain_reserved_usd']=str(reserve);self.persist();self.guard()
        try:raw=self.call('messages',copy.deepcopy(p))
        except Exception as exc:raise Halt('transport_error') from exc
        stage.update(raw_response=copy.deepcopy(raw),received_utc=self.clock().isoformat());self.persist()
        try:amount=cost(raw,model,kind)
        except Exception as exc:raise Halt('usage_unknown') from exc
        stage['cost_estimate_usd']=str(amount)
        self.r['spent_estimate_usd']=str(Decimal(self.r['spent_estimate_usd'])+amount);self.r['uncertain_reserved_usd']='0';self.persist()
        if amount>reserve or (kind!='search' and raw['usage']['input_tokens']>reserved_input):raise Halt('budget_stop')
        self.guard()
        if raw.get('stop_reason')!='end_turn':raise Stop('Incomplete response: '+str(raw.get('stop_reason'))[:30])
        return raw

def fitted(q,docs,time,j,needs,gaps,model,kind='forecast',base=None):
    attempts=[]
    for limit in LIMITS:
        try:p=profile.payload(q,docs,time,limit,needs=needs)
        except Stop:
            attempts.append(dict(chars=limit,reason='source_char_limit'));continue
        body=json.loads(p['messages'][0]['content'])
        if base is None:
            body.update(research_needs=needs,research_limitations=gaps)
        else:
            other=copy.deepcopy(base);other['sources']=body['sources'];other['selection_contract']=body['selection_contract'];body=other
        if kind=='need':
            from research_enrichment import SYSTEM
            p['system']=SYSTEM
        p.update(model=model,max_tokens=OUT[kind],output_config={'effort':'high'})
        p['messages'][0]['content']=json.dumps(body,ensure_ascii=False,allow_nan=False)
        n=j.count(p);attempts.append(dict(chars=limit,tokens=n))
        if n<=30000:return p,attempts
    raise Stop('Selected context exceeds full input limit')

def error(exc):return dict(error_type=type(exc).__name__,reason=str(exc)[:160] if isinstance(exc,Stop) else 'Host validation failed')

def run(catalog,window,folder,*,call,clock,save,capture_initial,capture_follow,budget=Decimal('42')):
    validate(catalog,now=clock().isoformat(),window=window)
    if type(budget) is not Decimal or not budget.is_finite() or not 0<budget<=42:raise ValueError('Frozen finite budget')
    r=dict(version=VERSION,catalog_sha256=digest(catalog),window=copy.deepcopy(window),publication=False,
        cells=[dict(question_id=q['id'],arm=arm,status='not_started') for q in catalog['questions'] for arm in ('opus','fable')],
        stages=[],pairs=[],spent_estimate_usd='0',uncertain_reserved_usd='0',complete=False)
    j=Journal(r,call,clock,window,save,budget);j.persist()
    def cell(q,arm):return next(c for c in r['cells'] if c['question_id']==q['id'] and c['arm']==arm)
    try:
        for index,full in enumerate(catalog['questions']):
            q=public_question(full);pair=j.begin_pair(q,index)
            for arm in pair['order']:
                model=OPUS if arm=='opus' else FABLE
                a=dict(model=model,effort='high',documents=[],diagnostics=[],research_status='started');pair['arms'][arm]=a
                dest=Path(folder)/q['id']/arm;dest.mkdir(parents=True,exist_ok=True)
                try:
                    p=research_payload(q,model);p.update(max_tokens=OUT['search'],output_config={'effort':'high'})
                    body=json.loads(p['messages'][0]['content']);body['research_time_utc']=clock().isoformat();p['messages'][0]['content']=json.dumps(body,ensure_ascii=False)
                    raw=j.request(arm,'search',p);plan,urls,searched=plan_urls(q,raw)
                    if any(len(n)>1000 for n in plan['needs']):raise Stop('Research need length')
                    needs=[dict(id='n'+str(i+1),description=n,required=True) for i,n in enumerate(plan['needs'])]
                    a.update(research_plan=plan,needs=needs,needs_sha256=digest(needs));j.persist();j.guard()
                    captured=capture_initial(q,urls,searched,dest);a['initial_capture']=captured;a['documents']=captured['documents'];j.persist();j.guard()
                    if not a['documents']:raise Stop('No readable source')
                    offered=candidates(q,needs,a['documents'],dest);a['offered_links']=offered
                    for turn in range(2):
                        effective=[]
                        try:
                            def generate(old):
                                b=json.loads(old['messages'][0]['content'])
                                request,attempts=fitted(q,a['documents'],clock().isoformat(),j,needs,[],model,kind='need',base=b)
                                effective.append(dict(request=request,token_fit=attempts))
                                return j.request(arm,'need',request)
                            result=assess(q,needs,a['documents'],offered if turn==0 else [],generate,
                                research_time=clock().isoformat(),final=bool(turn),model=model)
                            # assess's legacy parser built a Medium draft; only this substituted request was sent.
                            result['request']=effective[-1]['request'];result['token_fit']=effective[-1]['token_fit']
                            a.setdefault('assessments',[]).append(result);j.persist()
                            if turn or not result['selected_urls']:break
                            j.guard();extra=capture_follow(offered,result['selected_urls'],a['documents'],dest)
                            a['child_capture']=extra;a['documents']+=extra['documents'];j.persist();j.guard()
                        except Halt:raise
                        except Exception as exc:
                            a['diagnostics'].append(dict(stage='need',**error(exc)));break
                    a['research_status']='captured'
                except Halt:raise
                except Exception as exc:a.update(research_status='research_error',**error(exc))
                a.update(research_frozen_utc=clock().isoformat(),documents_sha256=digest(a['documents']));j.persist()
            pair['research_frozen_utc']=clock().isoformat();j.persist()
            for arm in pair['order']:
                a=pair['arms'][arm];c=cell(q,arm);model=a['model']
                if not a['documents']:c['status']='research_error';j.persist();continue
                try:
                    gaps=copy.deepcopy(a['diagnostics'])
                    if a.get('assessments'):gaps.append(dict(needs_ledger=a['assessments'][-1]['ledger']))
                    p,attempts=fitted(q,a['documents'],a['research_frozen_utc'],j,a['needs'],gaps,model)
                    a.update(forecast_request=p,token_fit=attempts);j.persist()
                    raw=j.request(arm,'forecast',p);answer=text_answer_check(raw,q,p)
                    a['raw_answer']=answer;c.update(status=answer['decision'],answer=copy.deepcopy(answer));j.persist()
                    if answer['decision']=='abstain':continue
                    docs=profile.audit_documents(p);rp=profile.review_payload(q,answer,docs);rp['max_tokens']=OUT['review']
                    a['review_request']=rp;j.persist()
                    try:
                        review_raw=j.request(arm,'review',rp);a['review']=profile.review_check(review_raw,q,answer,docs)
                        c['review_status']=a['review']['decision']
                    except Halt:raise
                    except Exception as exc:c['review_status']='review_error';a['review_error']=error(exc)
                except Halt:raise
                except Exception as exc:c.update(status='format_error',**error(exc))
                j.persist()
            j.finish_pair()
        r['complete']=True
    except Halt as exc:
        r['stop']=exc.status
        for c in r['cells']:
            if c['status']=='not_started':c['status']=exc.status
    except Exception as exc:r.update(stop='orchestration_error',**error(exc))
    j.persist();return r
