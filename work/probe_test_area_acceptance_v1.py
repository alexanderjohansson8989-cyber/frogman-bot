"""At most five authenticated GETs. No exposed write or model endpoint."""
import datetime as dt
import json
import os
import re
from candidate_api_v1 import JSONClient
from independent_runner import Stop
from test_area_acceptance_v1 import ACTOR, prepare

LISTING = 'posts/?tournaments=32977&forecast_type=binary&statuses=open&limit=20&include_descriptions=true'


class ReadOnlyClient:
    def __init__(self, token, *, opener=None):
        if not isinstance(token, str) or not token.strip():
            raise Stop('Metaculus credential missing')
        self.count = 0
        self.http = JSONClient('https://www.metaculus.com/api/',
            {'Authorization': 'Token '+token, 'Accept': 'application/json',
             'User-Agent': 'Frogman-bot/acceptance-preflight-v1'}, timeout=30, opener=opener)

    def get(self, path):
        if self.count >= 5 or (path not in ('users/me/', LISTING)
                and not re.fullmatch(r'posts/[1-9][0-9]*/\?include_descriptions=true', path)):
            raise Stop('Read-only probe path or call limit')
        self.count += 1
        return self.http.call(path, method='GET')


def run(client, *, clock):
    me_raw = client.get('users/me/')
    me = {k: me_raw.get(k) for k in ('id', 'username')}
    if me != ACTOR:
        raise Stop('Unexpected acting account')
    page = client.get(LISTING)
    rows = page.get('results')
    if not isinstance(rows, list) or len(rows) > 20:
        raise Stop('Bounded test listing required')
    ids = sorted({p['id'] for p in rows if isinstance(p, dict) and type(p.get('id')) is int
                  and isinstance(p.get('question'), dict) and p['question'].get('type') == 'binary'})
    result = dict(version='acceptance-preflight-v2', actor=me, observed_utc=clock().isoformat(),
                  listing_ids=ids, listing_has_next=bool(page.get('next')), attempts=[], plan=None,
                  forecast_posts=0, comment_posts=0, claude_calls=0)
    for pid in ids[:3]:
        post = client.get(f'posts/{pid}/?include_descriptions=true')
        groups = post.get('projects', {})
        project_fields = ('id', 'type', 'slug', 'name')
        def project(p):
            return {k:p[k] for k in project_fields if k in p} if isinstance(p, dict) else None
        memberships = {k:([project(p) for p in v] if isinstance(v,list) else project(v))
                       for k,v in groups.items()} if isinstance(groups,dict) else {}
        try:
            result['plan'] = prepare(post, me, listing_post_ids=ids, now=clock())
        except Stop as exc:
            result['attempts'].append(dict(post_id=pid, reason=str(exc), projects=memberships))
            continue
        # Keep only fields consumed by the contract, not other users' forecasts.
        q = post['question']
        keep = ('id','type','title','description','resolution_criteria','fine_print','status','resolution',
                'open_time','scheduled_close_time','actual_close_time','spot_scoring_time',
                'scheduled_resolve_time','actual_resolve_time','my_forecasts')
        result['selected_input'] = dict(id=post['id'],status=post['status'],
            projects=memberships,
            question={k:q[k] for k in keep if k in q})
        break
    result['get_attempts'] = client.count
    result['status'] = 'plan_prepared_not_submitted' if result['plan'] else 'no_eligible_test_question'
    return result


def main():
    result = run(ReadOnlyClient(os.environ.get('METACULUS_TOKEN', '')),
                 clock=lambda: dt.datetime.now(dt.timezone.utc))
    print('ACCEPTANCE_PREFLIGHT_RESULT '+json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
