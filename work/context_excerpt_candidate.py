"""Opt-in excerpt candidate. Exact source slices, no networking or summaries.

The old selector remains the default. Scores are retrieval heuristics, never
evidence that a page is correct or that missing facts do not exist.
"""
import copy
import json
import re
import html
from bounded_evidence import MAX_SENT_CHARS, excerpt
from independent_runner import Stop
from research_terms_v1 import words as tokens

VERSION = 'context-data-windows-3-criteria'
STOP = set('will what when where which before after with from that this have been than there their question resolution source criteria scoring deadline expected original first latest http https www html true false null com org'.split())
MONTH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'


def _terms(question, needs):
    # Criteria are retrieval hints, not evidence. Strip markup and URLs while
    # retaining visible series/definition text; never ingest scoring metadata.
    criteria=[]
    for key in ('criteria','fine_print'):
        text=question.get(key) or ''
        if not isinstance(text,str):continue
        text=html.unescape(text)
        text=re.sub(r'<[^>]*>',' ',text)
        criteria.append(re.sub(r'https?://[^\s<>\x22\x27)]+',' ',text,flags=re.I))
    value = [question.get('question',''), question.get('unit',''),
             criteria,[n.get('description','') for n in needs]]
    return tokens(json.dumps(value)) - STOP


def _score(text, terms):
    lines = [' '.join(x.split()) for x in text.splitlines() if x.strip()]
    normalized = ' '.join(lines)
    words=tokens(normalized)
    overlap = len(words & terms)
    numeric = sum(bool(re.search(r'(?<!\w)-?\d+(?:[.,]\d+)?',x)) for x in lines)
    # Navigation is usually lexical but has few measurements or full sentences.
    data = min(10,numeric) * .8
    dates = min(2,len(re.findall(MONTH+r'\s+\d{1,2}(?:[–—-]\d{1,2})?,?\s+20\d\d',normalized,re.I)))
    schedule = bool(re.search(r'(?:next|scheduled|would be held|will be held).{0,100}(?:meeting|'+MONTH+r')',normalized,re.I))
    table = bool(re.search(r'(?:seasonally adjusted|observed monthly|in thousands|weekly claims|sunspot number)',normalized,re.I))
    heading_words=tokens(normalized[:500])
    return overlap*2 + len(heading_words & terms)*2 + data + dates*2 + schedule*12 + (4 if table and numeric>=5 else 0)


def select_context_excerpt(document, question, char_limit, needs=()):
    if type(char_limit) is not int or not 1<=char_limit<=MAX_SENT_CHARS:
        raise Stop('Invalid excerpt limit')
    pages=document['pages'];total=sum(len(p['text']) for p in pages)
    if not total or not any(p['text'].strip() for p in pages):raise Stop('No readable source')
    if total<=char_limit:
        out=excerpt(document,char_limit);out['selection']=VERSION;return out
    terms=_terms(question,needs);candidates=[]
    for pi,p in enumerate(pages):
        text=p['text'];size=min(2400,char_limit)
        if p['page'] is not None and len(text)<=min(6000,char_limit):
            # PDF pages are natural units; keep header and bottom rows together.
            candidates.append((pi,0,len(text),_score(text,terms)+5))
        # Smaller overlapping windows permit relevant sentences near old cuts.
        for start in range(0,len(text),max(1,size//2)):
            lo=max(0,start-200);hi=min(len(text),lo+size)
            candidates.append((pi,lo,hi,_score(text[lo:hi],terms)))
        # Preserve table heading/period/units with a whole block of data. Raw
        # whitespace is retained; no rendered table alignment is invented.
        for m in re.finditer(r'(?im)^[ \t]*(?:(?:State|Week Ending|Industry|Category)[ \t\r]*$|Year[ \t]+Jan[^\n]*|OBSERVED MONTHLY (?:SUNSPOT|SOLAR|VALUES)[^\n]*)',text):
            lo=max(0,m.start()-250)
            block_size=3200 if re.search(r'Year|OBSERVED MONTHLY',m.group()) else 5500
            hi=min(len(text),lo+min(block_size,char_limit))
            part=text[lo:hi]
            if sum(bool(re.search(r'\d',x)) for x in part.splitlines())>=10:
                # Rank a table by its own heading, not terms in later sections.
                candidates.append((pi,lo,hi,_score(part[:900],terms)+12))
    ranked=sorted(candidates,key=lambda x:(-x[3],x[0],x[1],x[2]))
    chosen=[];remaining=char_limit
    for pi,lo,hi,score in ranked:
        intervals=[(lo,hi)]
        for op,a,b in chosen:
            if op!=pi:continue
            intervals=[z for x,y in intervals for z in
                       ([(x,y)] if b<=x or a>=y else
                        ([(x,a)] if x<a else [])+([(b,y)] if b<y else []))]
        # Preserve whole ranked blocks; don't silently chop off their headers.
        if sum(b-a for a,b in intervals)>remaining:continue
        for a,b in intervals:
            if a<b:chosen.append((pi,a,b));remaining-=b-a
        if remaining==0:break
    if not chosen:return excerpt(document,char_limit)
    chosen.sort();merged=[]
    for pi,a,b in chosen:
        if merged and merged[-1][0]==pi and merged[-1][2]==a:
            merged[-1]=(pi,merged[-1][1],b)
        else:merged.append((pi,a,b))
    spans=[dict(page=pages[pi]['page'],start=a,end=b,text=pages[pi]['text'][a:b]) for pi,a,b in merged]
    sent=sum(len(s['text']) for s in spans)
    out={k:copy.deepcopy(v) for k,v in document.items() if k!='pages'}
    out.update(spans=spans,selection=VERSION,selection_terms=sorted(terms),
               sent_chars=sent,extracted_chars=total,truncated=sent<total,
               omitted_chars=total-sent,absence_claims_allowed=False)
    return out


def forecast_payload_candidate(question,documents,research_time,per_source_chars=12000,needs=()):
    """Explicit opt-in request adapter; legacy live runner is unchanged.

    Returns an unapproved request: caller still owes full token counting and
    reservation. Audit adapter reads these same slices, never the hidden pages.
    """
    from bounded_evidence import forecast_payload,digest
    if any('frozen_shared_excerpt' in d for d in documents):
        raise Stop('Cannot replace a frozen shared excerpt')
    request=forecast_payload(question,documents,research_time,per_source_chars)
    grouped={}
    for d in documents:
        key=d['extracted_sha256']
        if key not in grouped:
            grouped[key]=select_context_excerpt(d,question,per_source_chars,needs)
            grouped[key]['source_aliases']=[]
        grouped[key]['source_aliases'].append({k:d[k] for k in ('url','original_sha256','retrieved_at')})
    if sum(d['sent_chars'] for d in grouped.values())>MAX_SENT_CHARS:raise Stop('Total source text limit')
    body=json.loads(request['messages'][0]['content']);body['sources']=list(grouped.values())
    body['selection_contract']=dict(version=VERSION,needs_sha256=digest(list(needs)),semantic_verification=False)
    request['messages'][0]['content']=json.dumps(body,ensure_ascii=False,allow_nan=False)
    return request
