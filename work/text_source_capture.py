"""One bounded read of bot-selected public HTTPS sources. No API or retries."""
import base64
import datetime as dt
import hashlib
import ipaddress
import socket
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from bounded_evidence import extract,MAX_BYTES
from independent_runner import NoRedirect,Stop
from operations_ledger_v1 import GlobalStop
from source_failure_v1 import describe
from source_http_v1 import fetch as fetch_source
from question_source_links_v1 import criterion_links


class ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.parts=[];self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.hidden+=1
        if not self.hidden and tag in ('p','div','h1','h2','h3','h4','tr','br','li'):
            self.parts.append('\n')
        if not self.hidden and tag in ('td','th'):self.parts.append('\t')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden:self.parts.append(data)


def public_https(url,resolver=socket.getaddrinfo):
    p=urlsplit(url)
    if (p.scheme!='https' or not p.hostname or p.username or p.password
            or p.port not in (None,443) or p.fragment):raise Stop('Public HTTPS URL required')
    addresses=resolver(p.hostname,443,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise Stop('Non-public source address')
    return url


def capture(url,folder,*,open_request=None,resolver=socket.getaddrinfo):
    public_https(url,resolver)
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    name=hashlib.sha256(url.encode()).hexdigest()+'.source'
    target=folder/name
    if target.exists():raise Stop('Source already captured; no silent overwrite')
    open_request=open_request or urllib.request.build_opener(NoRedirect()).open
    fetched=fetch_source(url,open_request=open_request,validate_url=lambda u:public_https(u,resolver))
    raw=fetched['body'];media=fetched['media_type']
    target.write_bytes(raw)
    wire_name=None
    if fetched['wire']!=raw:
        wire_name=hashlib.sha256(url.encode()).hexdigest()+'.wire.source'
        (folder/wire_name).write_bytes(fetched['wire'])
    captured=dt.datetime.now(dt.timezone.utc).isoformat()
    if raw.startswith(b'%PDF-'):
        src=dict(type='base64',media_type='application/pdf',data=base64.b64encode(raw).decode())
    elif media in ('text/html','text/plain','text/csv','application/json','application/xml','text/xml'):
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError as exc:raise Stop('Unsupported source encoding; original archived') from exc
        if media=='text/html':
            parser=ReadableHTML();parser.feed(text);text=''.join(parser.parts)
        src=dict(type='text',media_type='text/plain',data=text)
    else:raise Stop('Unsupported media type; original archived')
    doc=extract(url,src,captured)
    doc.update(network_original_sha256=hashlib.sha256(raw).hexdigest(),
               archive_name=name,network_media_type=media,
               network_original_bytes=len(raw),network_fetch_verified=True)
    doc.update(network_final_url=fetched['final_url'],network_redirects=fetched['redirects'],
               network_content_encoding=fetched['content_encoding'],
               network_wire_sha256=hashlib.sha256(fetched['wire']).hexdigest(),
               network_wire_bytes=len(fetched['wire']),wire_archive_name=wire_name)
    return doc


def capture_selected(question,selected_urls,searched_urls,folder,**kwargs):
    if not isinstance(selected_urls,list) or not 1<=len(selected_urls)<=3 or len(set(selected_urls))!=len(selected_urls):
        raise Stop('Expected one to three distinct selected URLs')
    allowed=set(searched_urls)
    explicit=question.get('resolution_source')
    if isinstance(explicit,str):allowed.add(explicit)
    criterion={item['url']:item for item in criterion_links(question)}
    allowed.update(criterion)
    if any(u not in allowed for u in selected_urls):raise Stop('Source URL lacks search/question provenance')
    documents=[];failures=[]
    for url in selected_urls:
        try:
            d=capture(url,folder,**kwargs)
            d['candidate_origin']='search_result' if url in searched_urls else 'exact_question_resolution_source'
            if url in criterion:
                d['candidate_origin']='question_criteria_link'
                d['question_link_provenance']=criterion[url]
                d['also_returned_by_search']=url in searched_urls
            documents.append(d)
        except GlobalStop:
            raise
        except Exception as error:
            # No automatic paid attempt, alternate address or HTTP bypass.
            failures.append(dict(url=url,**describe(error),readable=False))
    return dict(documents=documents,failures=failures,source_attempts=len(selected_urls))
