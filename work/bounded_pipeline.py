"""Question-driven research-plan -> captured sources -> bounded text request.

No paid calls here. A future runner owns search/generation budgets and records.
Never constructs fake web-fetch result blocks for the legacy validator.
"""
import json,re
from independent_runner import Stop,strict_json
from bounded_evidence import forecast_payload
from text_source_capture import capture_selected


def plan_urls(question,reply):
    text='\n'.join(b.get('text','') for b in reply.get('content',[]) if b.get('type')=='text')
    matches=re.findall(r'<research>\s*(.*?)\s*</research>',text,re.S)
    if len(matches)!=1:raise Stop('Research plan missing')
    p=strict_json(matches[0])
    if set(p)!={'question_id','needs','source_urls'} or p['question_id']!=question['id']:
        raise Stop('Research plan identity')
    if not isinstance(p['needs'],list) or not 1<=len(p['needs'])<=3 or any(not isinstance(n,str) or not n.strip() for n in p['needs']):
        raise Stop('Research needs missing')
    if not isinstance(p['source_urls'],list) or not 1<=len(p['source_urls'])<=3 or any(not isinstance(u,str) for u in p['source_urls']):
        raise Stop('Research URLs missing')
    searched=set()
    for b in reply.get('content',[]):
        if b.get('type')=='web_search_tool_result' and isinstance(b.get('content'),list):
            searched.update(x['url'] for x in b['content'] if isinstance(x,dict) and isinstance(x.get('url'),str))
    return p,list(dict.fromkeys(p['source_urls'])),searched


def prepare(question,reply,folder,research_time,**capture_options):
    plan,selected,searched=plan_urls(question,reply)
    sources=capture_selected(question,selected,searched,folder,**capture_options)
    result=dict(question=question,research_plan=plan,capture=sources,
                publication=False,factual_review_pending=True)
    if sources['documents']:
        result['request']=forecast_payload(question,sources['documents'],research_time)
        result['status']='text_request_prepared'
    else:result['status']='no_readable_source'
    return result


def audit_documents(request):
    """Read-only adapter to existing audit's text contract, with retained provenance."""
    docs=[]
    for d in json.loads(request['messages'][0]['content'])['sources']:
        text='\n[EXCERPT BOUNDARY]\n'.join(s['text'] for s in d['spans'])
        for alias in d['source_aliases']:
            docs.append(dict(url=alias['url'],text=text,truncated=d['truncated'],
                source_sha256=alias['original_sha256'],source_selection=d['selection'],
                source_spans=[{k:v for k,v in s.items() if k!='text'} for s in d['spans']]))
    return docs
