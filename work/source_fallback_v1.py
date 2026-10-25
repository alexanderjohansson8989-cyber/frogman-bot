"""At most two alternative search hits after zero readable selected sources.

No new search, URL invention, retry of failed addresses, change of user-agent,
or replacement of already readable sources. The ordinary capture guards apply.
"""
import copy
from urllib.parse import urlsplit

VERSION='bounded-source-alternatives-v1'
MAX_ALTERNATIVES=2

def alternatives(reply,selected,searched):
    selected=set(selected);allowed=set(searched);seen=set(selected)
    hosts={urlsplit(u).hostname for u in selected};out=[]
    for block in reply.get('content',[]):
        if block.get('type')!='web_search_tool_result' or not isinstance(block.get('content'),list):continue
        for item in block['content']:
            url=item.get('url') if isinstance(item,dict) else None
            if not isinstance(url,str) or url not in allowed or url in seen:continue
            seen.add(url)
            try:p=urlsplit(url)
            except ValueError:continue
            if p.scheme!='https' or not p.hostname or p.hostname in hosts:continue
            # DNS/public-address and the complete URL contract are enforced by capture.
            hosts.add(p.hostname);out.append(url)
            if len(out)==MAX_ALTERNATIVES:return out
    return out

def capture_with_alternatives(question,selected,searched,reply,folder,*,capture,guard):
    guard();first=capture(question,selected,searched,folder);guard()
    if first['documents']:return first
    urls=alternatives(reply,selected,searched)
    result=copy.deepcopy(first)
    result['fallback']=dict(version=VERSION,reason='no_readable_initial_source',selected_urls=urls,
        new_model_calls=0,max_alternative_reads=MAX_ALTERNATIVES)
    if not urls:return result
    guard();extra=capture(question,urls,searched,folder);guard()
    result['documents']+=extra['documents'];result['failures']+=extra['failures']
    result['source_attempts']+=extra['source_attempts']
    return result
