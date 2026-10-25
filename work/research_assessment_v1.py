"""Validate complete need assessments against the exact request that was sent.

Link selection is independent of individual finding validity. No network, no
repair of unfinished JSON, and no support borrowed from unseen originals.
"""
import copy,json,re
from bounded_evidence import digest
from evidence_quote_spans import locate
from independent_runner import Stop,strict_json
from need_gap_classification_v1 import CONTRACT as GAP_CONTRACT,classify

VERSION='research-assessment-v2'
FIELDS={'need_id','source_sha256','quote','kind','series','unit','period','target_relevance','vintage_status'}

def parse(reply,request):
    if reply.get('stop_reason')!='end_turn':raise Stop('Incomplete research assessment')
    body=strict_json(request['messages'][0]['content']);needs=body['needs'];sources=body['sources']
    if not isinstance(needs,list) or not 1<=len(needs)<=5 or len({n['id'] for n in needs})!=len(needs):raise Stop('Invalid frozen needs')
    ids={n['id'] for n in needs}
    text='\n'.join(b.get('text','') for b in reply.get('content',[]) if b.get('type')=='text')
    hits=re.findall(r'<research_quality>\s*(.*?)\s*</research_quality>',text,re.S)
    try:
        if len(hits)==1:
            a=strict_json(hits[0])
        elif not hits and text.strip().startswith('{') and text.strip().endswith('}'):
            # Complete bare JSON is the same envelope, not a repair of truncation.
            # All field, URL and exact-source checks below still apply.
            a=strict_json(text.strip())
        else:raise Stop('Research quality assessment missing')
    except ValueError as exc:raise Stop('Invalid research assessment JSON') from exc
    gap_contract=body.get('assessment_contract')==GAP_CONTRACT
    envelopes=({'findings','selected_urls'},{'findings','selected_urls','missing_needs'}) if gap_contract else ({'findings','selected_urls'},)
    if not isinstance(a,dict) or set(a) not in envelopes:raise Stop('Assessment envelope')
    if not isinstance(a['findings'],list) or len(a['findings'])>15:raise Stop('Bounded finding list required')
    selected=a['selected_urls'];diagnostics=[]
    allowed={c['url'] for c in body['candidates']}
    if (not isinstance(selected,list) or len(selected)>2 or any(not isinstance(u,str) for u in selected)
        or len(set(selected))!=len(selected) or any(u not in allowed for u in selected)
        or (body['final'] and selected)):
        selected=[];diagnostics.append(dict(scope='links',reason='Invalid or unlisted link selection'))
    # A good link cannot conceal a malformed finding. Keep it while recording
    # the finding as unusable, without inventing a quote or selecting a context.
    findings=[];covered=set()
    for index,f in enumerate(a['findings']):
        try:
            if (not isinstance(f,dict) or set(f)!=FIELDS or any(not isinstance(f[k],str) or not f[k].strip() for k in FIELDS)
                or f['need_id'] not in ids or f['kind'] not in ('observation','external_forecast','method','navigation')
                or f['vintage_status'] not in ('consistent','uncertain','conflicting') or not 15<=len(f['quote'])<=2000):
                raise Stop('Finding shape/context')
            positions=[];seen=set()
            for di,d in enumerate(sources):
                hashes={d['original_sha256']}|{z['original_sha256'] for z in d.get('source_aliases',[])}
                if f['source_sha256'] not in hashes:continue
                for si,span in enumerate(d['spans']):
                    for pos in locate(span['text'],f['quote']):
                        identity=(d['extracted_sha256'],span['page'],span['start']+pos['start'],span['start']+pos['end'])
                        if identity in seen:continue
                        seen.add(identity)
                        if len(positions)>=256:raise Stop('Finding position limit')
                        positions.append(dict(document_index=di,span_index=si,page=span['page'],
                            start=span['start']+pos['start'],end=span['start']+pos['end'],
                            original_quote=pos['original_quote'],match=pos['match'],
                            extracted_sha256=d['extracted_sha256'],source_aliases=copy.deepcopy(d.get('source_aliases',[]))))
            if not positions:raise Stop('Quote absent from sent spans')
            ambiguous=len(positions)>1
            findings.append(dict(finding=copy.deepcopy(f),positions=positions,context_selection_pending=ambiguous))
            # Multiple distinct contexts require a context-specific quotation.
            if not ambiguous and f['kind']=='observation' and f['vintage_status']=='consistent':covered.add(f['need_id'])
        except Stop as exc:diagnostics.append(dict(scope='finding',index=index,reason=str(exc)))
    ledger=dict(version=VERSION,question_sha256=digest(body['question']),request_sha256=digest(request),
        findings=findings,covered_observation_need_ids=sorted(covered),
        required_observation_needs_missing=[n['id'] for n in needs if n['required'] and n['id'] not in covered],
        semantic_review_pending=True,publication=False)
    if gap_contract:
        labels,issues=classify(a.get('missing_needs'),ledger['required_observation_needs_missing'],ids)
        ledger.update(version=GAP_CONTRACT,missing_need_classifications=labels)
        diagnostics.extend(issues)
    return dict(version=GAP_CONTRACT if gap_contract else VERSION,request_sha256=digest(request),selected_urls=copy.deepcopy(selected),
                ledger=ledger,diagnostics=diagnostics)
