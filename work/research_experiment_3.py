"""Independent bounded research arms. No credentials, submission or retries.

Caller supplies transport, public-source capture, clock and durable save.
Every paid request is journalled before transport and its original before parse.
"""
import copy
import json
import re
from decimal import Decimal
from pathlib import Path
from advisor_policy import OPUS,FABLE,RATES
from autonomous_runner import research_payload,usage_cost
from bounded_evidence import digest,forecast_payload,text_answer_check
from bounded_pipeline import plan_urls,audit_documents
from general_evaluation import instant
from independent_runner import Stop
from research_enrichment import assess
from research_quality import linked_candidates
from forecast_review import payload as review_payload,check as review_check

PAIR_RESERVE=Decimal('6.614')
LIMITS=(12000,8000,5000,3000)

class Halt(Stop):
    def __init__(self,status):super().__init__(status);self.status=status

def validate(catalog,window):
    if catalog.get('publication') is not False or catalog.get('version')!='research-catalog-3':raise ValueError('Development catalog')
    if digest(catalog['questions'])!=catalog['question_sha256'] or digest(catalog['needs'])!=catalog['needs_sha256']:raise ValueError('Catalog changed')
    if not instant(window['opens_utc'])<instant(window['closes_utc']):raise ValueError('Window')
    qs=catalog['questions']
    if len(qs)!=8 or len({q['id'] for q in qs})!=8 or len({q['target_key'] for q in qs})!=8:raise ValueError('Eight distinct targets')
    if any(q['target_key'] in catalog['excluded_target_keys'] or instant(q['outcome_not_before_utc'])<=instant(window['closes_utc']) for q in qs):raise ValueError('Prior or nonfuture target')
    if set(catalog['needs'])!={q['id'] for q in qs}:raise ValueError('Needs identity')

class Journal:
    def __init__(self,record,call,clock,window,save,budget):
        self.r=record;self.call=call;self.clock=clock;self.window=window;self.save=save;self.budget=budget
    def persist(self):self.save(copy.deepcopy(self.r))
    def guard(self):
        if not instant(self.window['opens_utc'])<=self.clock()<instant(self.window['closes_utc']):raise Halt('time_stop')
    def count(self,p):
        self.guard()
        result=self.call('messages/count_tokens',{k:copy.deepcopy(v) for k,v in p.items() if k not in ('max_tokens','output_config')})
        self.guard();n=result.get('input_tokens')
        if type(n) is not int or n<1:raise Stop('Invalid token preflight')
        return n
    def request(self,name,p,*,search=False):
        self.guard();p=copy.deepcopy(p);model=p['model']
        if model not in (OPUS,FABLE) or p['output_config']!={'effort':'medium'}:raise Stop('Model/effort')
        maxout=2500 if search else (3500 if name.endswith('need') else (2500 if name.endswith('review') else 6000))
        if type(p['max_tokens']) is not int or not 1<=p['max_tokens']<=maxout:raise Stop('Output bound')
        expected={'model','system','messages','output_config','max_tokens'}|({'tools'} if search else set())
        if set(p)!=expected or not isinstance(p['system'],str) or any(set(m)!={'role','content'} or m['role']!='user' or not isinstance(m['content'],str) for m in p['messages']):raise Stop('Request contract')
        if search and p['tools']!=[{'type':'web_search_20250305','name':'web_search','max_uses':2}]:raise Stop('Search bounds')
        n=self.count(p)
        if n>30000:raise Stop('Full-input token limit')
        reserved_input=(n*120+99)//100
        a,b=RATES[model]
        reserve=(Decimal(reserved_input)*a+Decimal(p['max_tokens'])*b)/1000000
        if search:reserve=Decimal('1.20') if model==OPUS else Decimal('3.00')
        if Decimal(self.r['spent_estimate_usd'])+reserve>self.budget:raise Halt('budget_stop')
        stage=dict(name=name,payload=p,payload_sha256=digest(p),preflight_input=n,reserved_input=reserved_input,reserve_usd=str(reserve),started_utc=self.clock().isoformat())
        self.r['stages'].append(stage);self.r['uncertain_reserved_usd']=str(reserve);self.persist();self.guard()
        try:raw=self.call('messages',copy.deepcopy(p))
        except Exception as exc:raise Halt('transport_error') from exc
        stage['raw_response']=copy.deepcopy(raw);stage['received_utc']=self.clock().isoformat();self.persist()
        try:
            u=raw['usage'];tools=u.get('server_tool_use') or {}
            if u.get('inference_geo','global')!='global' or any(v for k,v in tools.items() if k!='web_search_requests') or (not search and any(tools.values())):raise Stop('Unexpected billed tool/geography')
            amount=usage_cost(raw,model)
        except Exception as exc:raise Halt('usage_unknown') from exc
        stage['cost_estimate_usd']=str(amount)
        self.r['spent_estimate_usd']=str(Decimal(self.r['spent_estimate_usd'])+amount)
        self.r['uncertain_reserved_usd']='0';self.persist()
        if amount>reserve or (not search and u['input_tokens']>reserved_input) or u['output_tokens']>p['max_tokens']:raise Halt('budget_stop')
        self.guard() # Late original/usage are saved, but not accepted as an answer.
        if raw.get('stop_reason')!='end_turn':raise Stop('Incomplete response')
        return raw

def fitted_request(q,docs,time,journal,*,model=OPUS,needs=None,gaps=None):
    attempts=[]
    for limit in LIMITS:
        try:p=forecast_payload(q,docs,time,limit)
        except Stop:
            attempts.append(dict(limit=limit,reason='source_char_limit'));continue
        p['model']=model
        body=json.loads(p['messages'][0]['content'])
        if needs is not None:body['research_needs']=needs;body['research_limitations']=gaps or []
        p['messages'][0]['content']=json.dumps(body,ensure_ascii=False,allow_nan=False)
        n=journal.count(p);attempts.append(dict(limit=limit,tokens=n))
        if n<=30000:return p,attempts
    raise Stop('Arm excerpts exceed input limit')

def candidates(q,needs,docs,folder):
    from research_quality import known_urls
    from research_terms_v1 import words
    found=[];seen=known_urls(docs)
    for d in docs:
        if d.get('network_media_type')!='text/html':continue
        for c in linked_candidates(d,(Path(folder)/d['archive_name']).read_bytes()):
            if c['url'] not in seen:found.append(c);seen.add(c['url'])
    terms=words(json.dumps([q,needs]))
    return sorted(found,key=lambda c:-len(terms&words(c['url'])))[:30]

def run(catalog,window,folder,*,call,clock,save,capture_initial,capture_follow,budget=Decimal('54')):
    validate(catalog,window)
    if type(budget) is not Decimal or not budget.is_finite() or not 0<budget<=54:raise ValueError('Concrete finite budget')
    r=dict(version='research-experiment-3',catalog_sha256=digest(catalog),window=window,publication=False,
        stages=[],pairs=[],cells=[dict(question_id=q['id'],arm=arm,status='not_started') for q in catalog['questions'] for arm in ('opus','fable')],spent_estimate_usd='0',uncertain_reserved_usd='0',complete=False)
    j=Journal(r,call,clock,window,save,budget);j.persist()
    def cell(q,arm):return next(c for c in r['cells'] if c['question_id']==q['id'] and c['arm']==arm)
    try:
        for index,q in enumerate(catalog['questions']):
            j.guard()
            if budget-Decimal(r['spent_estimate_usd'])<PAIR_RESERVE:raise Halt('budget_stop')
            pair=dict(question_id=q['id'],reservation_usd=str(PAIR_RESERVE),order=['opus','fable'] if index%2==0 else ['fable','opus'],arms={})
            r['pairs'].append(pair);j.persist()
            for arm in pair['order']:
                model=OPUS if arm=='opus' else FABLE;needs=catalog['needs'][q['id']]
                a=dict(model=model,documents=[],diagnostics=[],research_status='started');pair['arms'][arm]=a
                dest=Path(folder)/q['id']/arm;dest.mkdir(parents=True,exist_ok=True)
                try:
                    p=research_payload(q,model);body=json.loads(p['messages'][0]['content']);body['research_time_utc']=clock().isoformat();body['frozen_needs']=needs;p['messages'][0]['content']=json.dumps(body,ensure_ascii=False)
                    raw=j.request(q['id']+'/'+arm+'/search',p,search=True)
                    _,urls,searched=plan_urls(q,raw)
                    j.guard();captured=capture_initial(q,urls,searched,dest);a['initial_capture']=captured;a['documents']=captured['documents'];j.persist();j.guard()
                    if not a['documents']:raise Stop('No readable source')
                    offered=candidates(q,needs,a['documents'],dest);a['offered_links']=offered
                    for turn in range(2):
                        try:
                            # A model may assess only this arm's bounded excerpts.
                            assessment_docs=copy.deepcopy(a['documents'])
                            def generate(p):
                                # assess currently builds 6000 chars/source; compact before generation if needed.
                                body=json.loads(p['messages'][0]['content'])
                                for limit in (6000,5000,3000):
                                    body['sources']=json.loads(forecast_payload(q,assessment_docs,clock().isoformat(),limit)['messages'][0]['content'])['sources']
                                    p['messages'][0]['content']=json.dumps(body,ensure_ascii=False)
                                    if j.count(p)<=30000:return j.request(q['id']+'/'+arm+'/need',p)
                                raise Stop('Need assessment token limit')
                            result=assess(q,needs,assessment_docs,offered if turn==0 else [],generate,research_time=clock().isoformat(),final=turn==1,model=model)
                            a.setdefault('assessments',[]).append(result);j.persist()
                            if turn or not result['selected_urls']:break
                            j.guard();extra=capture_follow(offered,result['selected_urls'],a['documents'],dest)
                            a['child_capture']=extra;a['documents']+=extra['documents'];j.persist();j.guard()
                        except Halt:raise
                        except Exception as exc:
                            a['diagnostics'].append(dict(stage='need',error_type=type(exc).__name__,reason=str(exc)[:160] if isinstance(exc,Stop) else 'Assessment failed'));break
                    a['research_status']='captured'
                except Halt:raise
                except Exception as exc:
                    a['research_status']='research_error';a['error_type']=type(exc).__name__
                a['research_frozen_utc']=clock().isoformat();a['documents_sha256']=digest(a['documents']);j.persist()
            # Both research arms are sealed before any prediction.
            pair['research_frozen_utc']=clock().isoformat();j.persist()
            for arm in pair['order']:
                a=pair['arms'][arm];c=cell(q,arm)
                if not a['documents']:c['status']='research_error';j.persist();continue
                try:
                    gaps=copy.deepcopy(a['diagnostics'])
                    if a.get('assessments'):gaps.append(dict(needs_ledger=a['assessments'][-1]['ledger']))
                    p,attempts=fitted_request(q,a['documents'],a['research_frozen_utc'],j,needs=catalog['needs'][q['id']],gaps=gaps)
                    a['forecast_request']=p;a['token_fit']=attempts;j.persist()
                    raw=j.request(q['id']+'/'+arm+'/forecast',p)
                    answer=text_answer_check(raw,q,p);a['raw_answer']=answer;c.update(status=answer['decision'],answer=copy.deepcopy(answer));j.persist()
                    if answer['decision']=='abstain':continue
                    docs=audit_documents(p);rp=review_payload(q,answer,docs)
                    a['review_request']=rp;j.persist()
                    try:
                        review_raw=j.request(q['id']+'/'+arm+'/review',rp)
                        a['review']=review_check(review_raw,q,answer,docs);c['review_status']=a['review']['review']['decision']
                    except Halt:raise
                    except Exception as exc:c['review_status']='review_error';a['review_error_type']=type(exc).__name__
                except Halt:raise
                except Exception as exc:
                    c['status']='format_error';c['error_type']=type(exc).__name__
                j.persist()
        r['complete']=True
    except Halt as exc:
        r['stop']=exc.status
        for c in r['cells']:
            if c['status']=='not_started':c['status']=exc.status
    except Exception as exc:r['stop']='orchestration_error';r['error_type']=type(exc).__name__
    j.persist();return r
