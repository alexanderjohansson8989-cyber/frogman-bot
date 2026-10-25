"""One authenticated private-list GET plus one public-list GET; never writes."""
import datetime as dt,json,os
from candidate_api_v1 import MetaculusClient,JSONClient
from independent_runner import Stop
from bounded_evidence import digest

PRIVATE_PATH='comments/?post=43327&author=310015&is_private=true&sort=-created_at&limit=25&offset=0'
PUBLIC_PATH='comments/?is_private=false&sort=-created_at&limit=25&offset=0'

class OneRead:
    def __init__(self,http,path):self.http,self.path,self.count=http,path,0
    def call(self,path,*,method='GET',payload=None,permit=None):
        if self.count or path!=self.path or method!='GET' or payload is not None or permit is not None:
            raise Stop('Exactly one fixed read-only request permitted')
        self.count+=1
        return self.http.call(path,method='GET')

def run(token):
    client=MetaculusClient(token)
    client.http=OneRead(client.http,PRIVATE_PATH)
    page=client.comments(43327,310015)
    if not isinstance(page,dict) or not isinstance(page.get('results'),list) or len(page['results'])>25:
        raise Stop('Unexpected comment list response')
    result=dict(observed_utc=dt.datetime.now(dt.timezone.utc).isoformat(),private_path=PRIVATE_PATH,
        private_gets=client.http.count,private_list_received=True,private_results_count=len(page['results']),
        private_response_sha256=digest(page),private_next_present=bool(page.get('next')),
        forecasts_sent=0,comments_sent=0,claude_calls=0)
    # No private text or authentication headers are exported. A separate
    # unauthenticated request can supply a real public serializer example.
    public=OneRead(JSONClient('https://www.metaculus.com/api/',
        {'Accept':'application/json','User-Agent':'Frogman-bot/comment-schema-probe-v1'},timeout=30),PUBLIC_PATH)
    try:
        data=public.call(PUBLIC_PATH)
        rows=data.get('results')
        if not isinstance(rows,list) or len(rows)>25:raise Stop('Unexpected public page')
        examples=[dict(comment_id=r.get('id'),on_post=r.get('on_post'),
            included_forecast=r['included_forecast']) for r in rows
            if isinstance(r,dict) and isinstance(r.get('included_forecast'),dict)]
        result.update(public_gets=public.count,public_path=PUBLIC_PATH,public_forecast_examples=examples[:1],
            public_results_count=len(rows))
    except Exception as exc:
        result.update(public_gets=public.count,public_error_type=type(exc).__name__)
    return result

if __name__=='__main__':
    print('COMMENT_READONLY_RESULT '+json.dumps(run(os.environ.get('METACULUS_TOKEN','')),sort_keys=True,allow_nan=False))
