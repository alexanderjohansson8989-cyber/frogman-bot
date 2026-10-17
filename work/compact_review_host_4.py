"""Opt-in host4 candidate; old host3 and frozen run results remain unchanged."""
import copy,json,re
from bounded_evidence import digest
from independent_runner import Stop,strict_json
from evidence_quote_spans import bind
VERSION='compact-review-host-4-positioned-whitespace'

def check(reply,question,answer,documents):
    if reply.get('stop_reason')!='end_turn':raise Stop('Compact review did not finish')
    text='\n'.join(b.get('text','') for b in reply.get('content',[]) if b.get('type')=='text')
    hits=re.findall(r'<compact_review>\s*(.*?)\s*</compact_review>',text,re.S)
    if len(hits)!=1:raise Stop('Compact review incomplete')
    a=strict_json(hits[0]);facts=answer.get('facts',[])
    if not isinstance(a,dict) or set(a)!={'checks','unsupported','subjective'}:raise Stop('Compact review shape')
    for key in ('unsupported','subjective'):
        if not isinstance(a[key],list) or len(a[key])>6 or any(not isinstance(s,str) or not 1<=len(s.strip())<=512 for s in a[key]):raise Stop('Compact premise bound')
    if not isinstance(a['checks'],list) or len(a['checks'])!=len(facts):raise Stop('Every fact needs review')
    seen=set();bindings=[]
    for c in a['checks']:
        if not isinstance(c,dict) or set(c)!={'fact_index','supported','evidence'}:raise Stop('Compact fact shape')
        i=c['fact_index']
        if type(i) is not int or not 0<=i<len(facts) or i in seen or type(c['supported']) is not bool:raise Stop('Compact fact identity')
        seen.add(i);quotes=c['evidence']
        if not isinstance(quotes,list) or len(quotes)>4 or (c['supported'] and not quotes):raise Stop('Compact quote count')
        for qi,q in enumerate(quotes):
            if not isinstance(q,dict) or set(q)!={'url','quote'} or not isinstance(q['quote'],str) or not 15<=len(q['quote'])<=500:raise Stop('Compact quote shape')
            if q['url']!=facts[i]['url']:raise Stop('Fact source changed')
            bindings.append(dict(fact_index=i,quote_index=qi,
                                 positions=bind(documents,q['url'],q['quote'])))
    passed=not a['unsupported'] and all(c['supported'] for c in a['checks'])
    return dict(version=VERSION,question_sha256=digest(question),original_answer_sha256=digest(answer),
                documents_sha256=digest(documents),review=a,quote_bindings=bindings,
                decision='pass' if passed else 'reject',original_answer=copy.deepcopy(answer),
                usable_answer=copy.deepcopy(answer) if passed else None,
                semantic_review_is_model_judgement=True,publication=False)
