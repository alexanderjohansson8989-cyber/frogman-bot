"""Explicit opt-in integration of reviewed selection and assumption wording."""
from context_excerpt_candidate import forecast_payload_candidate
from bounded_pipeline import audit_documents
from compact_forecast_review_candidate_2 import payload as review_payload
from compact_review_host_3 import check as review_check

VERSION='research-profile-2-host3-candidate'
PARAMETER_INSTRUCTION='''For each numerical predictive assumption, state whether
it is a subjective choice for the future target or an empirical estimate.
An empirical estimate needs its cited source, observation period and calculation;
do not invent historical volatility, frequency or methodology. A subjective
label cannot turn an empirical claim into judgement. Distinguish observed,
estimated and predicted source values and original versus revised releases.
Mark a chosen future mean, spread or scenario probability as judgement when it
is not calibrated. Consider all mixture components, not just the central one.'''

def payload(question,documents,research_time,per_source_chars=12000,needs=()):
    p=forecast_payload_candidate(question,documents,research_time,per_source_chars,needs)
    p['system']+='\n'+PARAMETER_INSTRUCTION
    return p

# The existing audit adapter receives the exact selected text. The caller still
# owns token counting, reservation, frozen request and any semantic review.
