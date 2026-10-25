"""Explicit unscored API acceptance contract; never a competition candidate.

Pure binding/readback only. No HTTP, credentials, journal mutation or dispatch.
The diagnostic adapter and its prohibition on submission remain unchanged.
"""
import copy
import datetime as dt
import math
from bounded_evidence import digest
from competition_adapter_v1 import identity, question_data
from general_evaluation import instant
from independent_runner import Stop

VERSION = 'test-area-acceptance-v2-project-types'
AREA = 32977
ACTOR = {'id': 310015, 'username': 'Frogman-bot'}
COMMENT = ('API integration test in the unscored bot testing area. The 50% value '
           'is a fixed transport-test value, not a researched forecast. '
           'This test checks submission and readback only.')


def contract(post, me):
    if identity(me, ACTOR['username']) != ACTOR:
        raise Stop('Unexpected test actor')
    q = question_data(post)
    raw = post['question']
    if raw['type'] != 'binary':
        raise Stop('First acceptance test is binary only')
    # Official serializer groups by project.type, including question_series,
    # and exposes the actual default_project separately. IDs remain mandatory.
    groups = post.get('projects', {})
    if not isinstance(groups, dict):
        raise Stop('Explicit test-area membership required')
    projects = []
    for key in ('tournament', 'question_series'):
        values = groups.get(key, [])
        if isinstance(values, list):
            projects.extend(p for p in values if isinstance(p, dict))
    default = groups.get('default_project')
    if isinstance(default, dict) and default.get('type') in ('tournament','question_series'):
        projects.append(default)
    if not any(type(p.get('id')) is int and p['id'] == AREA for p in projects):
        raise Stop('Explicit test-area membership required')
    return dict(post_id=post['id'], question_id=raw['id'], actor=copy.deepcopy(ACTOR),
                tournament_id=AREA, question=q)


def prepare(post, me, *, listing_post_ids, now):
    c = contract(post, me)
    if c['post_id'] not in listing_post_ids:
        raise Stop('Authenticated test-area listing membership required')
    q = post['question']
    if post.get('status') != 'open' or q.get('status') != 'open' or q.get('resolution') is not None:
        raise Stop('Test question is not open')
    times = c['question']['timeline']
    cutoff = min(instant(times[k]) for k in ('scheduled_close_time', 'actual_close_time'))
    if not instant(times['open_time']) <= now < cutoff:
        raise Stop('Test question outside opening window')
    previous = q.get('my_forecasts')
    if not isinstance(previous, dict) or previous.get('latest', 'missing') is not None:
        raise Stop('Missing or existing authenticated forecast')
    # Spot time is retained as actual metadata, but is not an unscored-test cutoff.
    b = dict(c, mode='test_area_acceptance', question_contract_sha256=digest(c),
             cutoff_utc=cutoff.isoformat(), bound_at_utc=now.isoformat())
    plan = dict(version=VERSION, purpose='synthetic-unscored-protocol-test', binding=b,
                payload=[dict(question=c['question_id'], source='api', probability_yes=.5)],
                expected_forecast_values=[.5, .5],
                comment=dict(on_post=c['post_id'], text=COMMENT, is_private=True, included_forecast=True),
                max_forecast_posts=1, max_comment_posts=1, automatic_retry=False,
                approval_required=True, submission_enabled=False,
                valid_until_utc=min(cutoff, now + dt.timedelta(days=1)).isoformat())
    plan['plan_sha256'] = digest(plan)
    return plan


def check_plan(plan):
    if not isinstance(plan, dict) or plan.get('version') != VERSION:
        raise Stop('Explicit acceptance plan required')
    if digest({k: v for k, v in plan.items() if k != 'plan_sha256'}) != plan.get('plan_sha256'):
        raise Stop('Acceptance plan changed')
    b = plan['binding']
    if (b.get('mode') != 'test_area_acceptance' or b['actor'] != ACTOR
            or b['tournament_id'] != AREA or plan.get('purpose') != 'synthetic-unscored-protocol-test'
            or plan['payload'] != [dict(question=b['question_id'], source='api', probability_yes=.5)]
            or plan['expected_forecast_values'] != [.5, .5]
            or plan['comment'] != dict(on_post=b['post_id'], text=COMMENT, is_private=True, included_forecast=True)
            or plan.get('max_forecast_posts') != 1 or plan.get('max_comment_posts') != 1
            or plan.get('automatic_retry') is not False or plan.get('submission_enabled') is not False
            or plan.get('approval_required') is not True):
        raise Stop('Acceptance scope changed')
    if not instant(b['bound_at_utc']) < instant(plan['valid_until_utc']) <= min(
            instant(b['cutoff_utc']), instant(b['bound_at_utc']) + dt.timedelta(days=1)):
        raise Stop('Acceptance plan lifetime invalid')


def preflight(plan, post, me, *, listing_post_ids, now):
    check_plan(plan)
    if not instant(plan['binding']['bound_at_utc']) <= now < instant(plan['valid_until_utc']):
        raise Stop('Acceptance plan expired or clock reversed')
    fresh = prepare(post, me, listing_post_ids=listing_post_ids, now=now)
    if fresh['binding']['question_contract_sha256'] != plan['binding']['question_contract_sha256']:
        raise Stop('Acceptance question contract changed')
    return True


def verify_readback(plan, post, me, *, dispatch_started_utc, now):
    check_plan(plan)
    b = plan['binding']
    if digest(contract(post, me)) != b['question_contract_sha256']:
        raise Stop('Acceptance readback question contract changed')
    start = instant(dispatch_started_utc)
    if not instant(b['bound_at_utc']) <= start < instant(plan['valid_until_utc']):
        raise Stop('Invalid acceptance dispatch time')
    if not start <= now <= start + dt.timedelta(minutes=5):
        raise Stop('Acceptance readback window expired')
    latest = post['question'].get('my_forecasts', {}).get('latest')
    if not isinstance(latest, dict) or latest.get('author_id') != ACTOR['id'] or latest.get('question_id') != b['question_id']:
        raise Stop('Acceptance forecast identity missing')
    stamp = latest.get('start_time')
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        raise Stop('Acceptance timestamp missing')
    if not start.timestamp()-1 <= stamp < min(instant(plan['valid_until_utc']).timestamp(), now.timestamp()+1):
        raise Stop('Stale or late acceptance forecast')
    if latest.get('end_time') is not None:
        raise Stop('Acceptance forecast was withdrawn')
    values = latest.get('forecast_values')
    if (not isinstance(values, list) or len(values) != 2
            or any(type(x) not in (int, float) or not math.isfinite(x) or abs(x-.5) > 1e-9 for x in values)):
        raise Stop('Acceptance forecast values differ')
    return dict(verified=True, post_id=b['post_id'], question_id=b['question_id'],
                author_id=ACTOR['id'], forecast_start_time=stamp, values_sha256=digest(values))
