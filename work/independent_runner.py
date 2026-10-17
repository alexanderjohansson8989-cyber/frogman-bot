"""Bounded fictional diagnostics. No Metaculus transport or imported project data."""
import datetime
import hashlib
import json
import math
import os
import re
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

MODELS=('claude-haiku-5-5','claude-opus-5-5')
PRICES={MODELS[0]:(Decimal('.10'),Decimal('.50')),MODELS[1]:(Decimal('4'),Decimal('20'))}
MAX_INPUT=4000
MAX_OUTPUT=2000
BUDGET=Decimal('1')
EFFORT='medium'
BODY_LIMIT=1000000

class Stop(Exception):pass

def strict_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate JSON key')
            result[key]=value
        return result
    def invalid(value):raise ValueError('Non-finite JSON constant')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise Stop('Redirect refused')

def api(path,payload=None):
    if path not in ('messages','messages/count_tokens') and path not in tuple('models/'+m for m in MODELS):
        raise Stop('Endpoint refused')
    if path.startswith('models/') and payload is not None:raise Stop('Model metadata must be read only')
    if not path.startswith('models/') and payload is None:raise Stop('Payload required')
    key=os.environ.get('ANTHROPIC_API_KEY','').strip()
    if not key:raise Stop('Missing Claude secret')
    request=urllib.request.Request('https://api.anthropic.com/v1/'+path,
        data=None if payload is None else json.dumps(payload,allow_nan=False).encode(),
        headers={'x-api-key':key,'anthropic-version':'2023-06-01','Content-Type':'application/json'},
        method='GET' if payload is None else 'POST')
    with urllib.request.build_opener(NoRedirect()).open(request,timeout=120) as response:
        raw=response.read(BODY_LIMIT+1)
    if len(raw)>BODY_LIMIT:raise Stop('Response too large')
    result=strict_json(raw)
    if not isinstance(result,dict):raise Stop('Response object required')
    return result

def model_matches(requested,returned):
    return isinstance(returned,str) and re.fullmatch(re.escape(requested)+r'(?:-\d{8})?',returned) is not None

def cost(model,incoming,outgoing):
    if model not in PRICES or any(type(x) is not int or x<0 for x in (incoming,outgoing)):
        raise Stop('Invalid usage')
    a,b=PRICES[model]
    return (incoming*a+outgoing*b)/Decimal(1000000)

def payload(model,case,system):
    # Only the self-authored prompt goes to the API. Oracle/other answers never included.
    return {'model':model,'system':system,'messages':[{'role':'user','content':case['prompt']}],
            'max_tokens':MAX_OUTPUT,'output_config':{'effort':EFFORT}}

def verified_usage(reply,model,max_output=MAX_OUTPUT,max_input=MAX_INPUT):
    if type(max_output) is not int or not 0<max_output<=6000:raise Stop('Invalid output ceiling')
    if type(max_input) is not int or not 0<max_input<=16000:raise Stop('Invalid input ceiling')
    usage=reply.get('usage')
    if not isinstance(usage,dict) or not model_matches(model,reply.get('model')):raise Stop('Unexpected model or usage')
    incoming,outgoing=usage.get('input_tokens'),usage.get('output_tokens')
    if (type(incoming) is not int or not 0<incoming<=max_input
            or type(outgoing) is not int or not 0<=outgoing<=max_output):raise Stop('Unexpected token use')
    for name in ('cache_creation_input_tokens','cache_read_input_tokens'):
        if type(usage.get(name,0)) is not int or usage.get(name,0)!=0:raise Stop('Unexpected cache')
    tools=usage.get('server_tool_use') or {}
    if not isinstance(tools,dict) or any(type(v) is not int or v!=0 for v in tools.values()):raise Stop('Unexpected tool use')
    if usage.get('service_tier','standard')!='standard':raise Stop('Unexpected service tier')
    return incoming,outgoing

def close_value(value,expected,tolerance):
    if isinstance(expected,dict):
        return (isinstance(value,dict) and set(value)==set(expected)
                and all(close_value(value[k],v,tolerance) for k,v in expected.items()))
    return type(value) in (int,float) and math.isfinite(value) and abs(value-expected)<=tolerance

def check_answer(text,oracle):
    normalized=text.strip()
    fenced=re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```',normalized,re.DOTALL)
    if fenced:normalized=fenced.group(1)
    answer=strict_json(normalized)
    if not isinstance(answer,dict) or set(answer)!={'decision','value','reason','missing_item'}:raise ValueError('Wrong answer fields')
    if answer['decision'] not in ('answer','abstain') or not isinstance(answer['reason'],str) or not 10<=len(answer['reason'].strip())<=5000:
        raise ValueError('Invalid decision/explanation')
    if answer['decision']=='abstain':
        if answer['value'] is not None or not isinstance(answer['missing_item'],str) or len(answer['missing_item'].strip())<10:
            raise ValueError('Actionable abstention required')
    elif answer['missing_item'] is not None:raise ValueError('Unexpected missing item')
    correct=answer['decision']==oracle['expected_decision']
    if correct and answer['decision']=='answer':correct=close_value(answer['value'],oracle['expected_value'],oracle['absolute_tolerance'])
    return {'answer':answer,'numeric_or_decision_correct':correct,
            'transport_wrapper_removed':fenced is not None,
            'precision_caveat':('Precision and exact output keys specified before generation.' if oracle.get('precision_declared') is True
                                else 'Original prompts did not specify decimal precision; numerical mismatches require review before calling them reasoning errors.'),
            'explanation_review_pending':True,
            'real_world_accuracy_verified':False,'production_approved':False}

def redact(value):
    if isinstance(value,str):
        key=os.environ.get('ANTHROPIC_API_KEY','').strip()
        return value.replace(key,'[MASKERAT]') if key else value
    if isinstance(value,list):return [redact(v) for v in value]
    if isinstance(value,dict):return {redact(k):redact(v) for k,v in value.items()}
    return value

def checkpoint(folder,evidence):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    path=folder/'evidence.json';tmp=folder/'evidence.json.tmp'
    tmp.write_text(json.dumps(redact(evidence),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    tmp.replace(path)

def load_bundle(folder):
    folder=Path(folder)
    manifest=strict_json((folder/'manifest.json').read_bytes())
    docs={}
    for name in ('prompts.json','facit.json'):
        raw=(folder/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=manifest['files'][name]:raise Stop('Changed independent bundle')
        docs[name]=strict_json(raw)
    prompts,oracle=docs['prompts.json'],docs['facit.json']
    cases=prompts['cases'];keys=[c['case_id'] for c in cases]
    if (prompts.get('origin')!='self_authored_fictional_no_external_question_data'
            or len(keys)!=6 or len(set(keys))!=6 or oracle.get('not_for_model_input') is not True
            or {c['case_id'] for c in oracle['cases']}!=set(keys)):
        raise Stop('Invalid independent case set')
    return prompts,{c['case_id']:c for c in oracle['cases']},manifest

def run(prompts,oracle,manifest,folder,call=api,seed=None):
    if os.environ.get('GITHUB_RUN_ATTEMPT','1')!='1':raise Stop('No Re-run: inspect stored evidence first')
    evidence={'version':'independent-3' if prompts.get('version')==2 else 'independent-2','run_id':os.environ.get('GITHUB_RUN_ID','unknown'),
        'commit':os.environ.get('GITHUB_SHA','unknown'),
        'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'manifest':manifest,'prompts':prompts,'models':list(MODELS),'effort':EFFORT,
        'preflight':[],'cells':[],'spent_estimate_usd':'0','uncertain_reserved_usd':'0',
        'publication':False,'complete':False,'explanation_review_pending':True}
    checkpoint(folder,evidence)
    plans=[]
    total=Decimal('0')
    try:
        completed=set()
        if seed is not None:
            if (seed.get('run_id')!='38245210884' or seed.get('commit')!='b04094fab7a9ded91f86f8d4af51520d155faae0'
                    or seed.get('version')!='independent-1' or seed.get('prompts')!=prompts
                    or seed.get('manifest',{}).get('files')!=manifest['files']
                    or seed.get('effort')!=EFFORT or seed.get('uncertain_reserved_usd')!='0'
                    or len(seed.get('cells',[]))!=2):raise Stop('Unverified continuation source')
            for old in seed['cells']:
                model=old['model_requested'];cid=old['case_id']
                key=(cid,model)
                if cid!='bayes_signal' or model not in MODELS or key in completed or old.get('stop_reason')!='end_turn':
                    raise Stop('Unexpected source cell')
                incoming,outgoing=verified_usage({'model':old['model_returned'],'usage':old['usage']},model)
                amount=cost(model,incoming,outgoing)
                if Decimal(old['cost_estimate_usd'])!=amount:raise Stop('Source cost mismatch')
                cell=dict(old)
                cell['check']=check_answer(old['raw_text'],oracle[cid])
                cell['status']='replayed_saved_response'
                cell['source_run']=seed['run_id']
                if model==MODELS[0]:
                    cell['manual_review']='Reason gives exact 4/7; value .5714 is rounded. Strict tolerance fails, but this is not a wrong Bayes derivation.'
                evidence['cells'].append(cell);completed.add(key);total+=amount
            if total!=Decimal(seed['spent_estimate_usd']):raise Stop('Source total mismatch')
            evidence['source_run']=seed['run_id']
            evidence['inherited_spent_estimate_usd']=str(total)
            evidence['spent_estimate_usd']=str(total)
            checkpoint(folder,evidence)
        for model in MODELS:
            if not model_matches(model,call('models/'+model).get('id')):raise Stop('Model preflight failed')
        for index,case in enumerate(prompts['cases']):
            order=MODELS if index%2==0 else tuple(reversed(MODELS))
            for model in order:
                if (case['case_id'],model) in completed:continue
                p=payload(model,case,prompts['system'])
                token_payload={k:v for k,v in p.items() if k in ('model','system','messages')}
                count=call('messages/count_tokens',token_payload).get('input_tokens')
                if type(count) is not int or not 0<count<=MAX_INPUT:raise Stop('Input token limit exceeded')
                reservation=cost(model,MAX_INPUT,MAX_OUTPUT)
                plans.append((case,model,p,count,reservation))
                evidence['preflight'].append({'case_id':case['case_id'],'model':model,'counted_input_tokens':count,'reservation_usd':str(reservation)})
        if total+sum(x[4] for x in plans)>BUDGET:raise Stop('Full pilot exceeds budget')
        checkpoint(folder,evidence)
        for case,model,p,count,reservation in plans:
            if total+reservation>BUDGET:raise Stop('Next call exceeds budget')
            cell={'case_id':case['case_id'],'model_requested':model,'status':'reserved',
                  'counted_input_tokens':count,'reserved_usd':str(reservation)}
            evidence['cells'].append(cell);evidence['uncertain_reserved_usd']=str(reservation)
            checkpoint(folder,evidence)
            started=time.monotonic()
            try:reply=call('messages',p)
            except Exception:
                cell['status']='transport_error_uncertain_charge'
                raise Stop('Transport stopped; reservation retained, no retry') from None
            cell.update(model_returned=reply.get('model'),stop_reason=reply.get('stop_reason'),duration_seconds=round(time.monotonic()-started,3))
            blocks=reply.get('content')
            if isinstance(blocks,list):
                cell['raw_text']=''.join(b['text'] for b in blocks if isinstance(b,dict) and b.get('type')=='text' and isinstance(b.get('text'),str))
            cell['status']='response_received_usage_unverified';checkpoint(folder,evidence)
            try:incoming,outgoing=verified_usage(reply,model)
            except Stop:
                cell['status']='unexpected_usage_or_model'
                raise
            amount=cost(model,incoming,outgoing);total+=amount
            cell.update(usage={'input_tokens':incoming,'output_tokens':outgoing},cost_estimate_usd=str(amount),status='format_error')
            evidence.update(spent_estimate_usd=str(total),uncertain_reserved_usd='0')
            if reply.get('stop_reason')!='end_turn':
                cell['status']='incomplete_response';raise Stop('Incomplete response; inspect before continuing')
            try:cell['check']=check_answer(cell.get('raw_text',''),oracle[case['case_id']])
            except (ValueError,TypeError,KeyError,OverflowError):raise Stop('Invalid JSON/format; inspect before continuing') from None
            cell['status']='checked_against_synthetic_oracle';checkpoint(folder,evidence)
        evidence['complete']=True
        return evidence
    except Exception as exc:
        evidence['stopped']=True
        evidence['stop_reason']=str(exc) if isinstance(exc,Stop) else type(exc).__name__
        raise
    finally:checkpoint(folder,evidence)

def main():
    folder=os.environ['FROGMAN_EVIDENCE_DIR']
    try:
        prompts,oracle,manifest=load_bundle(os.environ['FROGMAN_INDEPENDENT_INPUT'])
        seed_path=Path(os.environ['FROGMAN_INDEPENDENT_INPUT'])/'seed.json'
        seed=strict_json(seed_path.read_bytes()) if seed_path.exists() else None
        result=run(prompts,oracle,manifest,folder,seed=seed)
        correct=sum(c['check']['numeric_or_decision_correct'] for c in result['cells'])
        note=f'# Oberoende metodtest\n\n{len(result["cells"])} svar; {correct} matchar matematiskt facit/beslut. Förklaringarnas sakgranskning återstår.\n\nBeräknad API-förbrukning: ${result["spent_estimate_usd"]}. Console avgör faktisk debitering. Ingen Metaculus-data eller publicering.\n'
        code=0
    except Exception:
        note='# Oberoende metodtest – stopp\n\nSe evidence.json för senaste sparade steg och eventuell osäker reservation. Inga automatiska återförsök. Inga råa API-fel eller nycklar skrivs till loggen.\n'
        code=1
    print(note)
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary,'a',encoding='utf-8') as out:out.write(note)
    return code

if __name__=='__main__':raise SystemExit(main())
