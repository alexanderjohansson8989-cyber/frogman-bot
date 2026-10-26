"""Explicit opt-in Opus High profile for the first integration candidate."""
import copy,json,math
from independent_runner import Stop
from advisor_policy import OPUS
from research_profile_2 import payload as old_payload
from bounded_pipeline import audit_documents
from compact_forecast_review_candidate_2 import payload as old_review
from classified_review_v1 import evaluate as review_check,SYSTEM as CLASSIFIED_REVIEW_SYSTEM
from research_enrichment import SYSTEM as NEED_SYSTEM
from numeric_diagnostics_v1 import raw_quantiles
from need_gap_classification_v1 import CONTRACT as GAP_CONTRACT,RULES as GAP_RULES

VERSION='frogman-candidate-profile-v9-early-probability-limits'
STAGE_LIMITS={'search':1,'need':1,'forecast':1,'review':1}
OUT={'search':6000,'need':6000,'forecast':6000,'review':4000}
EFFORT={'search':'high','need':'medium','forecast':'high','review':'medium'}
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
distribution. For numeric/discrete API questions the host may project CDF bucket
masses onto API limits and will log the original, the final CDF and distances.
For discrete_outcomes the numeric mixture is latent: final outcomes are the
provided bucket centers, not arbitrary real values. Read timeline and criteria
together; opening, closing and resolution dates can differ.
Use a concise rationale of 10-650 characters for the separate comment field.
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
    b.update(needs=needs,candidates=candidates,final=final,assessment_contract=GAP_CONTRACT)
    p['system']=NEED_SYSTEM.replace('findings and selected_urls.','findings, selected_urls and missing_needs.')+'\n'+GAP_RULES+'''\nKeep the JSON compact: at most SIX findings, each
quote 15-240 characters, and short contextual fields. Prefer distinctive quotes
with period/series context. Do not repeat the same finding for identical source
aliases. Unsupported quote formatting must not prevent a separate offered-link
selection. Return complete JSON without a prose preamble.'''
    p.update(max_tokens=OUT['need'],output_config={'effort':EFFORT['need']})
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
    return dict(kind=d['kind'],note='Components are independently truncated before mixing; no untruncated mean asserted. Quantiles describe the raw latent continuous mixture, not final API buckets or discrete outcomes.',
                raw_numeric_quantiles=raw_quantiles(d))

def check_distribution(answer,question=None):
    if answer['decision']!='forecast':return
    d=answer['distribution']
    if d['kind']=='categorical' and any(not math.isfinite(v) or v<=0 for v in d['probabilities'].values()):
        raise Stop('Unjustified zero categorical outcome')
    constraints=(question or {}).get('submission_constraints')
    if d['kind'] not in ('binary','categorical') or constraints is None:return
    lo=constraints.get('minimum_probability');hi=constraints.get('maximum_probability')
    if (type(lo) not in (int,float) or type(hi) not in (int,float)
            or not math.isfinite(lo) or not math.isfinite(hi) or not 0<=lo<=hi<=1):
        raise Stop('Invalid submission probability constraints')
    values=[d['probability']] if d['kind']=='binary' else d['probabilities'].values()
    if any(type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi for v in values):
        raise Stop('Probability outside supplied submission limits; no clipping')

def review_payload(question,answer,documents):
    p=old_review(question,answer,documents)
    p['system']=CLASSIFIED_REVIEW_SYSTEM+'\n'+REVIEW_RULES
    b=json.loads(p['messages'][0]['content']);b['distribution_summary']=distribution_summary(answer)
    # Same selected bytes/provenance can occur under several source URLs.
    # Share only their text; keep every alias and all its metadata explicit.
    grouped=[];seen={}
    for doc in b['documents']:
        key=json.dumps({k:v for k,v in doc.items() if k not in ('url','source_sha256')},sort_keys=True,ensure_ascii=False)
        if key not in seen:
            seen[key]=len(grouped);grouped.append(copy.deepcopy(doc))
        else:
            target=grouped[seen[key]]
            target.setdefault('source_aliases',[]).append({k:copy.deepcopy(v) for k,v in doc.items() if k!='text'})
    b['documents']=grouped
    p['system']+="\nA document source_aliases list gives additional URLs and provenance for exactly the same supplied text. For checks.evidence, use exactly the fact's own URL, including when that URL appears as an alias; never substitute another URL from the group. For issues.evidence, you may cite any supplied document URL or listed alias URL. No missing text may be inferred."
    p['messages'][0]['content']=json.dumps(b,ensure_ascii=False,allow_nan=False)
    p.update(model=OPUS,max_tokens=OUT['review'],output_config={'effort':'medium'})
    return p
