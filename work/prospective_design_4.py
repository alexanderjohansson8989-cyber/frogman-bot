"""Offline catalog boundary checks and exact equal-weight ensemble log score.

Not a runner, release-provenance verifier, or permission to call an API.
"""
import datetime as dt
import hashlib
import json
import math
from collections import Counter

def digest(value):
    raw=value if isinstance(value,bytes) else json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()

def instant(value):
    t=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if t.tzinfo is None:raise ValueError('Timezone required')
    return t.astimezone(dt.timezone.utc)

def validate(catalog, *, now, window=None):
    if catalog.get('version')!='prospective-catalog-4' or catalog.get('publication') is not False:
        raise ValueError('Own development catalog required')
    qs=catalog['questions'];ids=[q['id'] for q in qs];keys=[q['target_key'] for q in qs]
    if len(qs)!=10 or len(set(ids))!=10 or len(set(keys))!=10:raise ValueError('Ten distinct targets')
    if digest(qs)!=catalog['question_sha256']:raise ValueError('Question identity changed')
    if set(keys)&set(catalog['excluded_target_keys']):raise ValueError('Previously used target')
    if Counter(q['type'] for q in qs)!=dict(numeric=2,categorical=2,binary=3,discrete=3):raise ValueError('Response type coverage')
    if len({q['domain'] for q in qs})!=6:raise ValueError('Domain coverage')
    close=instant(catalog['forecast_close_utc'])
    if instant(now)>=close:raise ValueError('Catalog expired; no automatic rolling')
    if catalog.get('manual_needs') is not False or 'needs' in catalog:raise ValueError('Needs must be arm-generated')
    if catalog['arms']!=[dict(id='opus',model='claude-opus-5-5',effort='high'),dict(id='fable',model='claude-fable-5-1',effort='high')]:raise ValueError('Frozen whole-chain arms')
    if catalog['reviewer']!=dict(model='claude-opus-5-5',effort='medium',diagnostic_only=True):raise ValueError('Diagnostic reviewer')
    if catalog['ensemble_weights']!=[0.5,0.5]:raise ValueError('Fixed mixture')
    for q in qs:
        r=q['resolution'];earliest=instant(q['outcome_not_before_utc']);expected=instant(r['release_expected_utc']);deadline=instant(r['deadline_utc'])
        if not close<earliest<=expected<deadline:raise ValueError('Future target and exclusive collection deadline')
        if not all(isinstance(q[k],str) and q[k].strip() for k in ('question','criteria','resolution_source','cluster')):raise ValueError('Question contract')
        if not q['resolution_source'].startswith('https://'):raise ValueError('Explicit official source')
        if r['vintage'] not in ('first_release','catalog_snapshot'):raise ValueError('Vintage contract')
        if r['vintage']=='catalog_snapshot' and instant(r['capture_opens_utc'])!=expected:raise ValueError('Snapshot starts at declared window')
        if q['type']=='numeric':
            if q['scoring']!={'kind':'rounded_mass','step':0.1,'interval':'half_open_centered'} or q['lower_bound'] is not None or q['upper_bound'] is not None:raise ValueError('Numeric score contract')
        else:
            if q['scoring']!={'kind':'category_mass'}:raise ValueError('Category score contract')
            if q['type']!='binary' and (not q['choices'] or len(set(q['choices']))!=len(q['choices'])):raise ValueError('Distinct categories')
    if window is not None:
        if not instant(window['opens_utc'])<=instant(now)<instant(window['closes_utc'])<=close:raise ValueError('Active window within catalog cutoff')
    return dict(question_sha256=catalog['question_sha256'],questions=10,domains=6,
                clusters=len({q['cluster'] for q in qs}),semantic_validation=False,executable=False)

def ensemble_nll(a,b):
    """50/50 arithmetic mass mixture, not an average of negative log scores.

    Only discrete/rounded mass losses (>=0), not arbitrary density scores.
    None means no pair; a single surviving forecast is not the ensemble.
    """
    if a is None or b is None:return None
    if any(type(x) not in (int,float) or math.isnan(x) or x<0 for x in (a,b)):
        raise ValueError('Nonnegative mass loss required')
    low=min(a,b)
    if low==math.inf:return math.inf
    return low + math.log(2)-math.log1p(math.exp(-abs(a-b)))
