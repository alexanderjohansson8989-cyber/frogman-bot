"""Optional future research configuration and accounting; no API transport."""
from decimal import Decimal
import copy

OPUS = 'claude-opus-5-5'
FABLE = 'claude-fable-5-1'
SONNET = 'claude-sonnet-5-5'
BETA = 'advisor-tool-2026-03-01'
RATES = {OPUS: (Decimal('4'), Decimal('20')),
         FABLE: (Decimal('10'), Decimal('50')),
         SONNET: (Decimal('2'), Decimal('10'))}


def advisor_tool(executor, completed_calls=0):
    if executor not in (OPUS, SONNET):
        raise ValueError('Unreviewed executor/advisor pair')
    if type(completed_calls) is not int or completed_calls < 0:
        raise ValueError('Invalid conversation advisor count')
    if completed_calls >= 1:
        return None
    return {'type': 'advisor_20260301', 'name': 'advisor', 'model': FABLE,
            'max_uses': 1, 'max_tokens': 2048}


def continuation(reply):
    """Keep opaque blocks intact; caller still checks budget before resuming."""
    if not isinstance(reply.get('content'), list):
        raise ValueError('Missing response content')
    return {'role': 'assistant', 'content': copy.deepcopy(reply['content'])}


def advisor_calls(reply):
    return sum(b.get('type') == 'server_tool_use' and b.get('name') == 'advisor'
               for b in reply.get('content', []) if isinstance(b, dict))


def _tokens(row):
    names = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
             'cache_creation_input_tokens')
    values = {}
    for name in names:
        value = row.get(name, 0 if 'cache' in name else None)
        if type(value) is not int or value < 0:
            raise ValueError('Missing/invalid token usage')
        values[name] = value
    if values['cache_read_input_tokens'] or values['cache_creation_input_tokens']:
        raise ValueError('Cache accounting not implemented for this pilot')
    return values


def usage_cost(reply, executor, advisor=FABLE):
    """Standard uncached token estimate, excluding separately priced web search.

    Never add top-level executor totals to executor iterations a second time.
    Unknown accounting means retain the caller's full reservation, not zero cost.
    """
    if executor not in RATES or advisor != FABLE:
        raise ValueError('Unreviewed pricing')
    if reply.get('model') != executor:
        raise ValueError('Unexpected executor model')
    usage = reply.get('usage')
    if not isinstance(usage, dict) or usage.get('service_tier', 'standard') != 'standard':
        raise ValueError('Unreviewed usage/tier')
    top = _tokens(usage)
    rows = usage.get('iterations')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Per-model iterations required for advisor accounting')
    sums = dict.fromkeys(top, 0)
    total = Decimal('0')
    advisor_count = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid iteration')
        tokens = _tokens(row)
        if row.get('type') == 'message':
            model = executor
            for name in sums:
                sums[name] += tokens[name]
        elif row.get('type') == 'advisor_message' and row.get('model') == advisor:
            model = advisor
            advisor_count += 1
            if tokens['output_tokens'] > 2048:
                raise ValueError('Advisor output exceeded configured ceiling')
        else:
            raise ValueError('Unknown iteration/model')
        incoming, outgoing = RATES[model]
        total += (tokens['input_tokens'] * incoming + tokens['output_tokens'] * outgoing) / Decimal(1000000)
    if sums != top:
        raise ValueError('Executor totals do not reconcile')
    # Pending pause_turn calls can have no billed advisor iteration yet.
    if advisor_count > 1 or advisor_count > advisor_calls(reply):
        raise ValueError('Unexpected advisor call count')
    successful_results = sum(
        b.get('type') == 'advisor_tool_result'
        and isinstance(b.get('content'), dict)
        and b['content'].get('type') in ('advisor_result', 'advisor_redacted_result')
        for b in reply.get('content', []) if isinstance(b, dict))
    if successful_results > advisor_count:
        raise ValueError('Successful advisor result missing billed usage')
    return {'token_estimate_usd': str(total), 'advisor_iterations': advisor_count,
            'pending_or_failed_calls': advisor_calls(reply) - advisor_count,
            'retain_uncertain_reservation': advisor_calls(reply) != advisor_count,
            'console_debit_verified': False}
