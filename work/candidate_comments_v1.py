"""A separate, disabled-by-default private-comment transaction.

Requires the exact forecast already verified in the ledger and on the server.
Unknown outcomes are only read back; never resend or add an identifying marker.
"""
import copy
import json
import math
from bounded_evidence import digest
from competition_adapter_v1 import check_plan,verify_readback
from general_evaluation import instant
from operations_ledger_v1 import GlobalStop
from independent_runner import Stop
from candidate_dispatch_v1 import DispatchPermit

VERSION='candidate-private-comment-v1'
def comment_plan(forecast_plan,ledger,*,now):
    check_plan(forecast_plan);b=forecast_plan['binding']
    key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    row=ledger.db.execute('SELECT plan_sha,state FROM submissions WHERE key=?',(key,)).fetchone()
    if row!=(digest(forecast_plan),'verified'):raise GlobalStop('Verified identical forecast required before comment')
    payload=copy.deepcopy(forecast_plan['comment'])
    if payload.get('is_private') is not True or payload.get('included_forecast') is not True or not 10<=len(payload['text'])<=1500:
        raise Stop('Short private forecast comment required')
    return dict(version=VERSION,forecast_plan=copy.deepcopy(forecast_plan),payload=payload,
        prepared_utc=now.isoformat(),key=key+'/comment',publication=False)

def verify(plan,comment):
    fp=plan['forecast_plan'];b=fp['binding'];p=plan['payload'];f=comment.get('included_forecast')
    if (type(comment.get('id')) is not int or comment['id']<=0 or comment.get('on_post')!=b['post_id']
            or comment.get('author',{}).get('id')!=b['actor']['id'] or comment.get('is_private') is not True
            or comment.get('is_soft_deleted') is not False or comment.get('parent_id') is not None
            or comment.get('text')!=p['text'] or not isinstance(f,dict)):
        raise Stop('Comment identity/content/privacy mismatch')
    if not instant(plan['prepared_utc'])<=instant(comment['created_at'])<instant(b['cutoff_utc']):
        raise Stop('Stale or late comment')
    # Reuse the exact value/author/time checker with the originally frozen post
    # contract; attached forecast must be identical to the verified submission.
    if (f.get('author_id')!=b['actor']['id'] or f.get('question_id')!=b['question_id']
            or f.get('forecast_values')!=fp['expected_forecast_values']):
        raise Stop('Attached forecast differs')
    start=f.get('start_time')
    if (type(start) not in (int,float) or not math.isfinite(start) or f.get('end_time') is not None
            or not instant(b['bound_at_utc']).timestamp()-1<=start<instant(b['cutoff_utc']).timestamp()):
        raise Stop('Attached forecast time differs')
    return dict(comment_id=comment['id'],author_id=b['actor']['id'],post_id=b['post_id'],
        text_sha256=digest(comment['text']),private=True,verified=True)

def readback(plan,ledger,client):
    b=plan['forecast_plan']['binding'];matches=[]
    for offset in (0,25,50):
        page=client.comments(b['post_id'],b['actor']['id'],offset)
        if not isinstance(page,dict) or not isinstance(page.get('results'),list) or len(page['results'])>25:
            raise Stop('Bounded comment page required')
        for comment in page['results']:
            try:receipt=verify(plan,comment)
            except (Stop,ValueError,TypeError):continue
            matches.append(receipt)
        if not page.get('next'):break
        if offset==50:raise Stop('Comment readback incomplete; no retry')
    if len(matches)!=1:raise Stop('Exactly one matching private comment required; no retry')
    ledger.confirm_submission(plan['key'],plan,matches[0])
    return matches[0]

def submit(forecast_plan,host,client,*,enabled=False):
    if enabled is not True:raise GlobalStop('Comment submission disabled')
    host.effect_deadline=min(host.effect_deadline,instant(forecast_plan['binding']['cutoff_utc']))
    host.before_effect();me,post=client.fetch(forecast_plan['binding']['post_id'])
    verify_readback(forecast_plan,post,me)
    plan=comment_plan(forecast_plan,host.ledger,now=host.clock())
    host.ledger.prepare_submission(plan['key'],plan)
    host.before_effect();host.ledger.begin_submission(plan['key'],plan)
    permit=DispatchPermit(host.ledger,plan['key'],plan['payload'],kind='comment',before_effect=host.before_effect)
    try:client.create_comment(plan['payload'],permit=permit)
    except Exception:pass
    return readback(plan,host.ledger,client)
