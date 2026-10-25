"""Account known candidate usage independently of the requested output cap.

An over-limit answer still costs credits. Callers settle it before persisting
a stop. Unknown pricing dimensions remain uncertain. Historical experiment
accounting and its frozen output caps are intentionally untouched.
"""
from decimal import Decimal
from advisor_policy import OPUS,RATES
from independent_runner import Stop
from candidate_profile_v1 import OUT

def cost(reply,model,kind):
    if model!=OPUS or kind not in OUT or reply.get('model')!=model:
        raise Stop('Unexpected candidate model/stage')
    u=reply.get('usage',{});n=u.get('input_tokens');out=u.get('output_tokens')
    # Sanity bound for decoding known usage, NOT permission to request this
    # many tokens. OUT and reservations still govern dispatch and hard stops.
    if type(n) is not int or not 0<n<=200000 or type(out) is not int or not 0<=out<=200000:
        raise Stop('Unknown token accounting')
    if any(u.get(k,0)!=0 for k in ('cache_read_input_tokens','cache_creation_input_tokens')):
        raise Stop('Cache not budgeted')
    if u.get('service_tier','standard')!='standard' or u.get('inference_geo','global')!='global':
        raise Stop('Unexpected tier/geography')
    tools=u.get('server_tool_use') or {};searches=tools.get('web_search_requests',0)
    if type(searches) is not int or not 0<=searches<=2 or any(v for k,v in tools.items() if k!='web_search_requests') or (kind!='search' and searches):
        raise Stop('Unbudgeted server tool')
    if u.get('iterations'):raise Stop('Unexpected iterative/advisor accounting')
    a,b=RATES[model]
    return (Decimal(n)*a+Decimal(out)*b)/1000000+Decimal(searches)*Decimal('.01')
