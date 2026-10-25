"""Fixed endpoint HTTP clients. Writes require an explicit one-use permit.

Clients do not retry, follow redirects, read credentials from source files or
create a journal. The host must checkpoint dispatch and provide the permit.
"""
import copy
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from independent_runner import strict_json
from operations_ledger_v1 import GlobalStop
from advisor_policy import OPUS

class TransportNotStarted(GlobalStop):
    """Local validation/checkpoint failed before opening the network."""

class TransportOutcomeUnknown(GlobalStop):
    """Transport was entered; this is NOT proof the server received a write."""

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise GlobalStop('API redirect forbidden')

class JSONClient:
    def __init__(self,base,headers,*,timeout=90,opener=None):
        self.base,self.headers,self.timeout=base,headers,timeout
        self.opener=opener or urllib.request.build_opener(NoRedirect())
    def call(self,path,*,method='GET',payload=None,permit=None):
        try:
            raw=None if payload is None else json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
            if raw is not None and len(raw)>2000000:raise GlobalStop('Request byte limit')
            req=urllib.request.Request(self.base+path,data=raw,headers=self.headers,method=method)
            if method=='POST':
                if permit is None:raise GlobalStop('Checkpointed one-use permit required')
                permit(payload)
                from candidate_dispatch_v1 import DispatchPermit
                if type(permit) is DispatchPermit:permit.mark_http_entered(payload)
        except GlobalStop as exc:raise TransportNotStarted(str(exc)) from None
        except Exception:raise TransportNotStarted('API local request preparation failed') from None
        try:
            with self.opener.open(req,timeout=self.timeout) as response:
                data=response.read(8000001)
                if len(data)>8000000:raise GlobalStop('API response too large; outcome unknown')
                value=strict_json(data)
                if not isinstance(value,(dict,list)):raise GlobalStop('API JSON object/list required')
                return value
        except urllib.error.HTTPError as exc:raise TransportOutcomeUnknown('API HTTP '+str(exc.code)+'; no retry') from None
        except GlobalStop as exc:raise TransportOutcomeUnknown(str(exc)) from None
        except Exception:raise TransportOutcomeUnknown('API transport outcome unknown; no retry') from None

class ClaudeClient:
    def __init__(self,key,*,opener=None,timeout=300):
        if not isinstance(key,str) or not key.strip():raise GlobalStop('Claude credential missing')
        if type(timeout) is not int or not 1<=timeout<=300:raise GlobalStop('Bounded Claude timeout required')
        self.http=JSONClient('https://api.anthropic.com/v1/',
            {'x-api-key':key,'anthropic-version':'2023-06-01','Content-Type':'application/json'},opener=opener,timeout=timeout)
    def metadata(self):return self.http.call('models/'+OPUS)
    def count(self,payload):
        if payload.get('model')!=OPUS:raise GlobalStop('Unreviewed candidate model')
        body={k:copy.deepcopy(v) for k,v in payload.items() if k not in ('max_tokens','output_config')}
        # Count endpoint performs no generation. Its local permit only allows
        # this exact body once, with no paid endpoint exposed to the count path.
        result=self.http.call('messages/count_tokens',method='POST',payload=body,permit=lambda p:None)
        n=result.get('input_tokens')
        # The pipeline uses larger counts to shorten selected context. The
        # requester, not this counting endpoint, enforces the 30000 dispatch cap.
        if type(n) is not int or not 1<=n<=200000:raise GlobalStop('Full input count out of bounds')
        return n
    def generate(self,payload,*,permit=None):
        if payload.get('model')!=OPUS:raise GlobalStop('Unreviewed candidate model')
        return self.http.call('messages',method='POST',payload=payload,permit=permit)

class MetaculusClient:
    def __init__(self,token,*,opener=None):
        if not isinstance(token,str) or not token.strip():raise GlobalStop('Metaculus credential missing')
        self.http=JSONClient('https://www.metaculus.com/api/',
            {'Authorization':'Token '+token,'Content-Type':'application/json','Accept':'application/json','User-Agent':'Frogman-bot/candidate-v1'},timeout=30,opener=opener)
    @staticmethod
    def id(value):
        if type(value) is not int or value<=0:raise GlobalStop('Positive API identity required')
        return str(value)
    def me(self):
        raw=self.http.call('users/me/')
        return {k:raw[k] for k in ('id','username')}
    def post(self,post_id):return self.http.call('posts/'+self.id(post_id)+'/?include_descriptions=true')
    def fetch(self,post_id):return self.me(),self.post(post_id)
    def forecast(self,payload,*,permit=None):
        if not isinstance(payload,list) or len(payload)!=1:raise GlobalStop('One forecast only')
        self.id(payload[0]['question'])
        return self.http.call('questions/forecast/',method='POST',payload=payload,permit=permit)
    def create_comment(self,payload,*,permit=None):
        if (set(payload)!={'on_post','text','is_private','included_forecast'} or payload.get('is_private') is not True
                or payload.get('included_forecast') is not True or not isinstance(payload.get('text'),str)
                or not 10<=len(payload['text'])<=1500):raise GlobalStop('Short private forecast comment required')
        self.id(payload['on_post'])
        return self.http.call('comments/create/',method='POST',payload=payload,permit=permit)
    def comments(self,post_id,actor_id,offset=0):
        self.id(post_id);self.id(actor_id)
        if type(offset) is not int or offset not in (0,25,50):raise GlobalStop('Bounded comment readback')
        query=urllib.parse.urlencode(dict(post=post_id,author=actor_id,is_private='true',sort='-created_at',limit=25,offset=offset))
        return self.http.call('comments/?'+query)
