"""Bounded public HTTPS redirects and gzip; no access-control bypass or retry."""
import zlib
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request
from independent_runner import Stop
from bounded_evidence import MAX_BYTES

REDIRECTS={301,302,303,307,308}
MAX_REDIRECTS=2

def fetch(url,*,open_request,validate_url):
    current=url;chain=[];seen=set()
    for step in range(MAX_REDIRECTS+1):
        validate_url(current)
        if current in seen:raise Stop('Source redirect loop')
        seen.add(current)
        req=Request(current,headers={'User-Agent':'Frogman-bot/1.0 (forecasting research)','Accept-Encoding':'identity'})
        try:response=open_request(req,timeout=30)
        except HTTPError as exc:
            if exc.code not in REDIRECTS:raise
            location=exc.headers.get('Location');exc.close()
            if not location or step==MAX_REDIRECTS:raise Stop('Source redirect bound or missing target')
            following=urljoin(current,location)
            # Validate before any redirected HTTP request; no cookies/auth are forwarded.
            validate_url(following);chain.append(dict(source=current,target=following));current=following
            continue
        with response:
            if response.geturl()!=current:raise Stop('Uncontrolled source redirect')
            encoding=response.headers.get('Content-Encoding','identity').strip().lower()
            if encoding not in ('','identity','gzip'):raise Stop('Unsupported source content encoding')
            wire=response.read(MAX_BYTES+1)
            media=response.headers.get_content_type()
        if len(wire)>MAX_BYTES:raise Stop('Source byte limit; no partial evidence')
        raw=wire
        if encoding=='gzip':
            try:
                decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
                raw=decoder.decompress(wire,MAX_BYTES+1)
                if len(raw)>MAX_BYTES or decoder.unconsumed_tail:raise Stop('Decompressed source byte limit')
                if not decoder.eof or decoder.unused_data:raise Stop('Incomplete or concatenated source gzip')
            except zlib.error as exc:raise Stop('Invalid source gzip') from exc
        return dict(body=raw,wire=wire,media_type=media,content_encoding=encoding or 'identity',final_url=current,redirects=chain)
    raise Stop('Source redirect bound')
