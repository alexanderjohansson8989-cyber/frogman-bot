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
from operations_ledger_v1 import GlobalStop,readback_scope
from independent_runner import Stop
from candidate_dispatch_v1 import DispatchPermit,fixed_submission_client,record_submission_contract

VERSION='candidate-private-comment-v1'

def attached_forecast_start(f):
    # ForecastSerializer uses ISO datetime; MyForecastSerializer uses epoch.
    try:return instant(f['start_time']).timestamp()
    except (KeyError,ValueError,TypeError,OverflowError) as exc:
        raise Stop('Attached forecast ISO time required') from exc

def attached_values(fp,f):
    kind=fp['binding']['question']['api_type']
    if f.get('question_type')!=kind:raise Stop('Attached question type differs')
    if kind=='binary':
        p=f.get('probability_yes')
        if type(p) not in (int,float) or not math.isfinite(p) or not 0<=p<=1:
            raise Stop('Attached binary probability invalid')
        values=[1-p,p]
    elif kind=='multiple_choice':
        values=f.get('probability_yes_per_category')
        if f.get('options')!=fp['binding']['question']['choices']:
            raise Stop('Attached options differ')
    elif kind in ('numeric','discrete'):
        values=f.get('continuous_cdf')
    else:raise Stop('Unsupported attached forecast type')
    expected=fp['expected_forecast_values']
    if not isinstance(values,list) or len(values)!=len(expected):raise Stop('Attached forecast shape differs')
    for got,want in zip(values,expected):
        if want is None:
            if got is not None:raise Stop('Inactive option differs')
        elif (type(got) not in (int,float) or not math.isfinite(got)
                or not math.isclose(got,want,rel_tol=0,abs_tol=1e-9)):
            raise Stop('Attached forecast differs')
    return values
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
    # Identity belongs to the enclosing comment. The attachment is a
    # ForecastSerializer, not my_forecasts.latest; absent identity fields must
    # not be fabricated. Compare its type/values and timezone-aware ISO time.
    attached_values(fp,f)
    start=attached_forecast_start(f)
    if not instant(b['bound_at_utc']).timestamp()-1<=start<instant(b['cutoff_utc']).timestamp():
        raise Stop('Attached forecast time differs')
    return dict(comment_id=comment['id'],author_id=b['actor']['id'],post_id=b['post_id'],
        text_sha256=digest(comment['text']),private=True,verified=True)

def readback(plan,ledger,client,*,before_readback=None,verify_extra=None):
    with readback_scope(ledger,plan['key']):
        return _readback(plan,ledger,client,before_readback=before_readback,verify_extra=verify_extra)

def _readback(plan,ledger,client,*,before_readback=None,verify_extra=None):
    b=plan['forecast_plan']['binding'];matches=[]
    for offset in (0,25,50):
        if before_readback:before_readback()
        page=client.comments(b['post_id'],b['actor']['id'],offset)
        if before_readback:before_readback()
        if not isinstance(page,dict) or not isinstance(page.get('results'),list) or len(page['results'])>25:
            raise Stop('Bounded comment page required')
        for comment in page['results']:
            try:
                receipt=verify(plan,comment)
                if verify_extra:verify_extra(comment)
            except (Stop,ValueError,TypeError):continue
            matches.append(receipt)
        # Metaculus uses CountlessLimitOffsetPagination: next can exist on
        # the last or even an empty page. Inspect the ORIGINAL page length.
        if len(page['results'])<25 or not page.get('next'):break
        if offset==50:raise Stop('Comment readback incomplete; no retry')
    if len(matches)!=1:raise Stop('Exactly one matching private comment required; no retry')
    ledger.confirm_submission(plan['key'],plan,matches[0])
    return matches[0]

def submit(forecast_plan,host,client,*,enabled=False):
    import datetime as dt
    from candidate_api_v1 import TransportOutcomeUnknown
    if enabled is not True:raise GlobalStop('Comment submission disabled')
    host.effect_deadline=min(host.effect_deadline,instant(forecast_plan['binding']['cutoff_utc']))
    host.before_effect();me,post=client.fetch(forecast_plan['binding']['post_id'])
    verify_readback(forecast_plan,post,me)
    plan=comment_plan(forecast_plan,host.ledger,now=host.clock())
    host.ledger.prepare_submission(plan['key'],plan)
    if fixed_submission_client(client,'comment'):
        record_submission_contract(host.ledger,plan['key'],plan,kind='comment')
    host.before_effect();host.ledger.begin_submission(plan['key'],plan)
    permit=DispatchPermit(host.ledger,plan['key'],plan['payload'],kind='comment',before_effect=host.before_effect)
    read_deadline=min(host.expiry,host.clock()+dt.timedelta(seconds=90))
    try:client.create_comment(plan['payload'],permit=permit)
    except TransportOutcomeUnknown:pass
    except GlobalStop:raise
    except Exception:pass
    return readback(plan,host.ledger,client,before_readback=lambda:host.before_readback(read_deadline))
