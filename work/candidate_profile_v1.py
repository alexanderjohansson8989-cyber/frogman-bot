"""Explicit opt-in Opus High profile for the first integration candidate."""
import json,math
from independent_runner import Stop
from advisor_policy import OPUS
from research_profile_2 import payload as old_payload
from bounded_pipeline import audit_documents
from compact_forecast_review_candidate_2 import payload as old_review
from compact_review_host_4 import check as review_check
from research_enrichment import SYSTEM as NEED_SYSTEM

VERSION='frogman-candidate-profile-v1'
OUT={'search':6000,'need':6000,'forecast':12000,'review':4000}
FORECAST_RULES='''Use at most three SHORT atomic factual claims; each must be
supported by its own cited URL including any period, unit, category or vintage.
Do not combine a date from one source with data from another under one URL.
Research needs, search snippets and diagnostic notes are NOT evidence. Do not
use their unverified factual hints as supplied observations. Derive an empirical
estimate only from cited supplied observations with a stated calculation. A
future mean, spread, mechanism or scenario probability can be an explicit
subjective choice; never re-label an asserted historical frequency as subjective.
Keep future uncertainty separate from failure to obtain a resolution. Unresolved
is a workflow status, never an outcome or a reason to add mass to NO. For future
categorical outcomes assign strictly positive mass to every provided category;
do not make uncertain tails impossible. Abstain if this contract cannot be met.
Check the stated distribution against its described mean and marginal chances.
If submission_constraints are supplied, respect them when choosing the initial
distribution; the host will not clip probabilities, floor buckets or widen it.
Keep assumptions/gaps/rationale concise, without restating entire research logs.'''
REVIEW_RULES='''Review the exact supplied answer, not a stronger invented claim.
Check factual qualifiers, empirical assumptions, and numerical derivations.
A chosen future uncertainty or causal scenario is not a measured historical rate.
Flag unsupported empirical inputs even if they are called a subjective prior.
Flag mixing probability of unresolved with probability of NO, and contradictions
between the actual distribution summary and the stated mean/marginal chances.
Use at most two short evidence quotes per fact, copying a distinctive contiguous
span. Avoid repeating the same objection. Do not require the future outcome to
be observed before a forecast is made. Keep each note under 200 characters.'''

def payload(question,documents,time,limit=12000,needs=()):
    p=old_payload(question,documents,time,limit,needs=needs)
    p['system']+='\n'+FORECAST_RULES
    p.update(model=OPUS,max_tokens=OUT['forecast'],output_config={'effort':'high'})
    return p

def need_payload(question,documents,time,needs,candidates,*,final=False,limit=12000):
    p=payload(question,documents,time,limit,needs)
    b=json.loads(p['messages'][0]['content'])
    b.update(needs=needs,candidates=candidates,final=final)
    p['system']=NEED_SYSTEM+'''\nKeep the JSON compact: at most SIX findings, each
quote 15-240 characters, and short contextual fields. Prefer distinctive quotes
with period/series context. Do not repeat the same finding for identical source
aliases. Unsupported quote formatting must not prevent a separate offered-link
selection. Return complete JSON without a prose preamble.'''
    p.update(max_tokens=OUT['need'])
    p['messages'][0]['content']=json.dumps(b,ensure_ascii=False,allow_nan=False)
    return p

def distribution_summary(answer):
    d=answer['distribution']
    if d['kind']=='binary':return dict(kind='binary',probability_yes=d['probability'])
    if d['kind']=='categorical':
        p=d['probabilities'];out=dict(kind='categorical',probabilities=p)
        if all(k.isdigit() for k in p):out['mean']=sum(int(k)*v for k,v in p.items())
        elif all(k.isdigit() or k=='10_plus' for k in p):
            out['mean_lower_bound']=sum((10 if k=='10_plus' else int(k))*v for k,v in p.items())
        return out
    return dict(kind=d['kind'],note='Components are independently truncated before mixing; no untruncated mean asserted.')

def check_distribution(answer):
    if answer['decision']!='forecast':return
    d=answer['distribution']
    if d['kind']=='categorical' and any(not math.isfinite(v) or v<=0 for v in d['probabilities'].values()):
        raise Stop('Unjustified zero categorical outcome')

def review_payload(question,answer,documents):
    p=old_review(question,answer,documents)
    p['system']+='\n'+REVIEW_RULES
    b=json.loads(p['messages'][0]['content']);b['distribution_summary']=distribution_summary(answer)
    p['messages'][0]['content']=json.dumps(b,ensure_ascii=False,allow_nan=False)
    p.update(model=OPUS,max_tokens=OUT['review'],output_config={'effort':'medium'})
    return p
