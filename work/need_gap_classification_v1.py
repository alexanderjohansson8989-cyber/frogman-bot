"""Bounded model-reported gap labels, never evidence or a gate."""
CONTRACT='research-assessment-v3-gap-kinds'
KINDS=('missing_observation','future_release','future_uncertainty',
       'source_unavailable','irrelevant_source','conflicting_evidence','unknown')
RULES='''For each required need without an adequate supplied observation, add
one missing_needs entry with exactly need_id and gap_kind. Use these labels:
missing_observation: a relevant historical/current observation is absent;
future_release: the required observation is not scheduled to exist yet;
future_uncertainty: it concerns an unknown future outcome, not a missing fact;
source_unavailable: supplied capture information explicitly shows access failed;
irrelevant_source: supplied material addresses the wrong series, period or scope;
conflicting_evidence: supplied observations contradict one another;
unknown: the supplied information cannot distinguish the reason.
At most five entries, each need_id once. Do not infer an access failure from
absent text alone. These are tentative diagnostic labels, not verified facts.
They cannot waive evidence requirements, authorize searches or select extra URLs.
Do not include explanations, new facts or instructions in missing_needs.'''

def classify(value,missing,all_ids):
    """One record per host-derived missing need; bad labels degrade to unknown.

    Missing/duplicate/invalid classifications never erase findings or links.
    Even a model-reported future gap leaves host coverage unchanged.
    """
    diagnostics=[];seen={};bad=set()
    if not isinstance(value,list) or len(value)>5:
        diagnostics.append(dict(scope='gap_kind',reason='Missing or invalid bounded classification list'))
        value=[]
    for entry in value:
        if not isinstance(entry,dict) or not isinstance(entry.get('need_id'),str) or entry['need_id'] not in all_ids:
            diagnostics.append(dict(scope='gap_kind',reason='Unknown or malformed need identifier'));continue
        ident=entry['need_id']
        if ident in seen:bad.add(ident)
        seen[ident]=entry
        if set(entry)!={'need_id','gap_kind'} or not isinstance(entry.get('gap_kind'),str) or entry['gap_kind'] not in KINDS:
            bad.add(ident)
        if ident not in missing:
            diagnostics.append(dict(scope='gap_kind',need_id=ident,reason='Label ignored for a covered or non-required need'))
    result=[]
    for ident in missing:
        valid=ident in seen and ident not in bad
        status='model_reported' if valid else 'invalid' if ident in bad else 'not_reported'
        result.append(dict(need_id=ident,gap_kind=seen[ident]['gap_kind'] if valid else 'unknown',
                           classification_status=status,semantic_review_pending=True))
        if ident in bad:diagnostics.append(dict(scope='gap_kind',need_id=ident,reason='Invalid or duplicate label; kept unknown'))
    return result,diagnostics
