"""Read-only identity/schema probe. Two allowlisted GETs, no Claude requests."""
import datetime as dt,hashlib,json,os,urllib.request
from urllib.parse import urlsplit,parse_qsl
from pathlib import Path
from independent_runner import NoRedirect,Stop
from competition_adapter_v1 import bind_post

PATHS=('users/me/','posts/?tournaments=fall-futureeval-2026&limit=25&statuses=open&forecast_type=binary,multiple_choice,numeric&include_descriptions=true')
def page_path(url):
    parsed=urlsplit(url)
    if (parsed.scheme!='https' or parsed.netloc!='www.metaculus.com' or parsed.path!='/api/posts/' or parsed.fragment):raise Stop('Pagination origin/path changed')
    items=parse_qsl(parsed.query,keep_blank_values=True);params=dict(items)
    expected=dict(parse_qsl(PATHS[1].split('?',1)[1]))
    if (len(items)!=len(params) or set(params)-set(expected)!={'offset'} or any(params.get(k)!=v for k,v in expected.items())
        or not params['offset'].isdigit() or not 1<=int(params['offset'])<=250):raise Stop('Pagination filters changed')
    return 'posts/?'+parsed.query
def get(path):
    if path not in PATHS:page_path('https://www.metaculus.com/api/'+path)
    key=os.environ.get('METACULUS_TOKEN','').strip()
    if not key:raise Stop('Metaculus secret missing')
    req=urllib.request.Request('https://www.metaculus.com/api/'+path,headers={'Authorization':'Token '+key,'Accept':'application/json','User-Agent':'Frogman-bot/schema-check-v1'},method='GET')
    if req.get_method()!='GET':raise Stop('Read-only HTTP method required')
    with urllib.request.build_opener(NoRedirect()).open(req,timeout=30) as reply:
        raw=reply.read(3000001)
        if len(raw)>3000000:raise Stop('Probe body bound')
    return json.loads(raw)

def main(fetch=get):
    now=dt.datetime.now(dt.timezone.utc)
    result=dict(version='candidate-access-probe-v1',time_utc=now.isoformat(),publication=False,claude_calls=0,
                get_attempts=0,page_attempts=0,scope='at most three pages of 25 posts; not a complete tournament inventory',rows=[])
    try:
        result['get_attempts']+=1;me=fetch(PATHS[0])
        if me.get('username')!='Frogman-bot':raise Stop('Unexpected acting account')
        # Preserve only public bot identity, never account contact/billing fields.
        result['actor']={k:me[k] for k in ('id','username')}
        path=PATHS[1];seen=set()
        for index in range(3):
            if path in seen:raise Stop('Pagination cycle')
            seen.add(path);result['get_attempts']+=1;result['page_attempts']+=1
            page=fetch(path);posts=page['results']
            if not isinstance(posts,list) or len(posts)>25:raise Stop('Post page shape')
            result['more_pages']=bool(page.get('next'))
            for post in posts:
                row=dict(post_id=post.get('id'),api_type=(post.get('question') or {}).get('type'),
                    post_keys=sorted(post),post_status=post.get('status'),
                    has_question=isinstance(post.get('question'),dict),
                    has_group=bool(post.get('group_of_questions')),has_conditional=bool(post.get('conditional')),
                    question_keys=sorted(post['question']) if isinstance(post.get('question'),dict) else [])
                try:
                    b=bind_post(post,me,tournament_id=33121,expected_username='Frogman-bot',now=now)
                    row.update(status='bound',binding=b)
                except Exception as exc:row.update(status='unavailable',error_type=type(exc).__name__,reason=str(exc)[:160] if isinstance(exc,Stop) else 'Required response field absent or invalid')
                result['rows'].append(row)
            if not page.get('next') or any(r['status']=='bound' for r in result['rows']):break
            if index<2:path=page_path(page['next'])
        result['status']='complete'
    except Exception as exc:result.update(status='probe_error',error_type=type(exc).__name__,reason=str(exc)[:160] if isinstance(exc,Stop) else 'Read failed; no raw error body printed')
    folder=Path(os.environ.get('FROGMAN_PROBE_OUTPUT','probe'));folder.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()
    (folder/'result.json').write_bytes(raw)
    summary={k:v for k,v in result.items() if k!='rows'}
    summary['rows']=[{k:v for k,v in r.items() if k!='binding'} for r in result['rows']]
    summary['result_sha256']=hashlib.sha256(raw).hexdigest()
    print('PROBE_RESULT '+json.dumps(summary,ensure_ascii=False,sort_keys=True))
    return result
if __name__=='__main__':main()
