"""One approved test forecast, exact readback, then one short private comment.

No models or retries. Caller supplies a fresh, fenced DurableHost. An interrupted
or uncertain state is retained for explicit inspection; never bootstrap over it.
This is deliberately separate from the competition adapter and diagnostic plans.
"""
import copy
import datetime as dt
from bounded_evidence import digest
from candidate_api_v1 import MetaculusClient, JSONClient, TransportOutcomeUnknown
from candidate_dispatch_v1 import DispatchPermit
from candidate_comments_v1 import readback as read_comment
from general_evaluation import instant
from operations_ledger_v1 import GlobalStop, readback_scope
from probe_test_area_acceptance_v1 import LISTING
from test_area_acceptance_v1 import check_plan, preflight, verify_readback


def execute(plan, host, client, *, approved_plan_sha256=None):
    check_plan(plan)
    if approved_plan_sha256 != plan['plan_sha256']:
        raise GlobalStop('Explicit approval of this exact test plan is required')
    if (type(client) is not MetaculusClient or type(client.http) is not JSONClient
            or client.http.base != 'https://www.metaculus.com/api/'):
        raise GlobalStop('Fixed Metaculus client required')
    if host.ledger.db.execute('SELECT 1 FROM operations').fetchone():
        raise GlobalStop('Acceptance journal must contain no model operations')
    host.effect_deadline=min(host.effect_deadline,instant(plan['valid_until_utc']))
    host.before_effect();me=client.me()
    host.before_effect();page=client.http.call(LISTING)
    rows=page.get('results')
    if not isinstance(rows,list) or len(rows)>20:raise GlobalStop('Bounded test listing required')
    ids=[r['id'] for r in rows if isinstance(r,dict) and type(r.get('id')) is int]
    b=plan['binding'];host.before_effect();post=client.post(b['post_id'])
    preflight(plan,post,me,listing_post_ids=ids,now=host.clock())
    ledger=host.ledger;key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    ledger.prepare_submission(key,plan)
    host.before_effect()
    started=host.clock()
    ledger.record('acceptance-dispatch-time',dict(key=key,started_utc=started.isoformat(),plan_sha256=plan['plan_sha256']))
    ledger.begin_submission(key,plan)
    # A deadline/transport failure here keeps the isolated journal. It must not
    # cause another POST. General N1 recovery remains a pre-production task.
    permit=DispatchPermit(ledger,key,plan['payload'],kind='forecast',before_effect=host.before_effect)
    try:client.forecast(copy.deepcopy(plan['payload']),permit=permit)
    except TransportOutcomeUnknown:pass
    read_deadline=started+dt.timedelta(minutes=5)
    guard=lambda:host.before_readback(read_deadline)
    with readback_scope(ledger,key):
        guard();me=client.me();guard();post=client.post(b['post_id']);guard()
        receipt=verify_readback(plan,post,me,dispatch_started_utc=started.isoformat(),now=host.clock())
        ledger.confirm_submission(key,plan,receipt)
    # Comment is a separate checkpointed transaction and cannot run before the
    # exact forecast has been independently confirmed.
    host.before_effect()
    cp=dict(version='test-area-private-comment-v1',forecast_plan=copy.deepcopy(plan),
            payload=copy.deepcopy(plan['comment']),prepared_utc=host.clock().isoformat(),
            key=key+'/comment',forecast_receipt=receipt,publication=False)
    ledger.prepare_submission(cp['key'],cp)
    host.before_effect();ledger.begin_submission(cp['key'],cp)
    permit=DispatchPermit(ledger,cp['key'],cp['payload'],kind='comment',before_effect=host.before_effect)
    try:client.create_comment(cp['payload'],permit=permit)
    except TransportOutcomeUnknown:pass
    # Common verifier binds author, privacy, text, attached values and time.
    # Additionally bind the exact already verified forecast timestamp below.
    def exact_forecast(comment):
        from independent_runner import Stop
        from candidate_comments_v1 import attached_forecast_start
        # Both serializers preserve microseconds. Allow floating-point
        # conversion error only, not a different forecast within one second.
        if abs(attached_forecast_start(comment['included_forecast'])-receipt['forecast_start_time'])>1e-6:
            raise Stop('Comment attached to a different forecast timestamp')
    comment_receipt=read_comment(cp,ledger,client,before_readback=guard,verify_extra=exact_forecast)
    return dict(status='verified',plan_sha256=plan['plan_sha256'],forecast=receipt,
                comment=comment_receipt,claude_calls=0,automatic_retry=False)
