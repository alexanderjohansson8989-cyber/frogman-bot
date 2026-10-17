"""Offline adapters into bounded text and deterministic count-answer resolution.

No search, generation, submission or live-runner mutation. A caller must lock
the count target contract before providing a new question to this candidate.
"""
import copy
import json
import math
import re
from bounded_evidence import extract, digest, forecast_payload
from autonomous_runner import validate_answer
from independent_runner import strict_json
from structured_series_candidate import project_months
from usgs_catalog_candidate import window_counts, utc
from count_distribution_candidate import count_distribution

NOAA_MONTHLY_URL = 'https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json'


def _document(url, raw, projection, retrieved_at, method):
    text = json.dumps(projection, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(text)>12000:
        raise ValueError('Structured view exceeds 12000 characters; reduce explicit selection')
    doc = extract(url, {'type':'text','data':text}, retrieved_at)
    # Source bytes differ from the deterministic derived text; retain both hashes.
    doc.update(original_sha256=digest(raw), original_bytes=len(raw),
               original_media_type='application/json', extraction_method=method,
               projection_sha256=digest(projection),
               derived_text_sha256=digest(text.encode('utf-8')),
               factual_review_pending=True)
    return doc


def monthly_document(raw, *, url, retrieved_at, start, end, fields):
    utc(retrieved_at)
    if url != NOAA_MONTHLY_URL:
        raise ValueError('Only reviewed NOAA monthly schema is supported')
    if any(field not in ('ssn','observed_swpc_ssn','smoothed_ssn','smoothed_swpc_ssn','f10.7','smoothed_f10.7') for field in fields):
        raise ValueError('Unknown NOAA field')
    projection=project_months(raw,time_field='time-tag',fields=fields,start=start,end=end)
    for row in projection['rows']:
        if any(type(row['values'][field]) not in (int,float) or not math.isfinite(row['values'][field]) for field in fields):
            raise ValueError('Invalid NOAA numeric field')
    projection['series_caution']='SSN/SWPC and smoothed/unsmoothed fields are distinct; -1 sentinels retained. Units/vintage require separate verification.'
    return _document(url,raw,projection,retrieved_at,'bounded_monthly_json_projection_1')


def catalogue_document(raw, request, *, retrieved_at, window_seconds):
    projection=window_counts(raw,request,retrieved_at=retrieved_at,window_seconds=window_seconds)
    # Transmit all aggregates/selection rules, not a clipped event list. Full event
    # rows stay reproducible from original bytes and the manifest recipe/hash.
    view={key:value for key,value in projection.items() if key!='capture'}
    view['source_selection']={key:value for key,value in projection['capture'].items()
                              if key not in ('events','excluded')}
    view['full_projection_sha256']=digest(projection)
    view['event_rows_recoverable_from_original']=True
    return _document(request['url'],raw,view,retrieved_at,'bounded_usgs_window_projection_1')


def count_contract(question, contract):
    if (not isinstance(contract,dict) or set(contract) != {'kind','overflow_at'}
            or contract['kind'] != 'unbounded_nonnegative_integer'
            or type(contract['overflow_at']) is not int or not 1 <= contract['overflow_at'] <= 100):
        raise ValueError('Explicit unbounded-count target contract required')
    n=contract['overflow_at']
    if question.get('type') not in ('discrete','categorical') or question.get('choices') != [str(i) for i in range(n)]+[str(n)+'_plus']:
        raise ValueError('Question choices do not match count target')
    return n


def count_payload(question, documents, research_time, contract):
    count_contract(question,contract)
    result=forecast_payload(question,documents,research_time,per_source_chars=12000)
    # Explicitly replace only the final answer contract; source safety retained.
    prefix=result['system'].split('Finish with exactly one JSON object',1)[0]
    result['system']=prefix+'''\nFinish with exactly one block: <count_proposal> followed by ONE syntactically
valid complete JSON object, followed by </count_proposal>. No Markdown fences.
The JSON object's top-level fields are:
question_id, decision (forecast or abstain), count_model, facts, assumptions, gaps,
rationale. Never supply category probabilities or a distribution field. For a forecast,
count_model is {"components":[...]}. Each component has label, family, weight and mean;
family is poisson or negative_binomial. A negative_binomial component has EXACTLY
these keys: label, family, weight, mean, dispersion. The JSON key is "dispersion",
NOT "k"; dispersion is the positive negative-binomial shape parameter k. Example:
{"label":"illustrative","family":"negative_binomial","weight":1,"mean":2,"dispersion":1}.
A poisson component has EXACTLY label, family, weight, mean, without dispersion.
Mean is the expected count for the TARGET interval, not an annual/daily rate.
Use one to five mutually exclusive scenarios; positive weights sum to one. This is
a mixture, not addition of background and aftershocks. Mean 0..100, k .01..1000000.
Host code calculates categories and total moments. State parameter uncertainty,
source/target differences and subjective transfer/scenario assumptions explicitly.
Facts: one to three objects with claim, url and observation_date (string or null).
Assumptions and gaps: arrays of at most eight strings. Rationale: 10..4000 characters.
For abstain, count_model is null. Historical observations are predictors; do not
invent catalogue frequencies from memory or treat revised data as old-vintage truth.
</count_proposal>'''
    body=json.loads(result['messages'][0]['content'])
    body['locked_count_contract']=copy.deepcopy(contract)
    result['messages'][0]['content']=json.dumps(body,ensure_ascii=False,allow_nan=False)
    return result


def resolve_count_reply(reply, question, request, contract):
    n=count_contract(question,contract)
    body=strict_json(request['messages'][0]['content'])
    if body.get('question') != question or body.get('locked_count_contract') != contract:
        raise ValueError('Request question/count contract changed')
    text='\n'.join(b.get('text','') for b in reply.get('content',[]) if b.get('type')=='text')
    matches=re.findall(r'<count_proposal>\s*(.*?)\s*</count_proposal>',text,re.S)
    if len(matches)==1:
        if text.count('<count_proposal>')!=1 or text.count('</count_proposal>')!=1:
            raise ValueError('Exactly one count proposal required')
        proposal=strict_json(matches[0])
    elif not matches and '<count_proposal' not in text and '</count_proposal' not in text:
        # Pure JSON/fenced pure JSON are equivalent wrappers, never repaired data.
        candidate=text.strip()
        fenced=re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```',candidate,re.S)
        if fenced:candidate=fenced.group(1)
        if not candidate.startswith('{') or not candidate.endswith('}'):
            raise ValueError('Exactly one count proposal required')
        proposal=strict_json(candidate)
    else:
        raise ValueError('Exactly one count proposal required')
    expected={'question_id','decision','count_model','facts','assumptions','gaps','rationale'}
    if not isinstance(proposal,dict) or set(proposal)!=expected:
        raise ValueError('Wrong count proposal fields')
    answer={key:copy.deepcopy(value) for key,value in proposal.items() if key!='count_model'}
    calculation=None
    if proposal['decision']=='abstain':
        if proposal['count_model'] is not None:
            raise ValueError('Abstention count model must be null')
        answer['distribution']=None
    else:
        model=proposal['count_model']
        if not isinstance(model,dict) or set(model)!={'components'}:
            raise ValueError('Count model components required')
        if not proposal['assumptions']:
            raise ValueError('Explicit count assumptions required')
        calculation=count_distribution(model['components'],n)
        answer['distribution']={'kind':'categorical','probabilities':dict(zip(question['choices'],calculation['probabilities']))}
    allowed={alias['url'] for source in body['sources'] for alias in source['source_aliases']
             if source.get('spans') and any(span['text'].strip() for span in source['spans'])}
    # Validate the derived answer using the same existing narrative/URL contract.
    derived_reply={'content':[{'type':'text','text':'<forecast>'+json.dumps(answer,allow_nan=False)+'</forecast>'}]}
    checked=validate_answer(derived_reply,question,allowed)
    return {'version':'computed-count-answer-1','raw_response_sha256':digest(reply),
            'proposal':copy.deepcopy(proposal),'answer':checked,'calculation':calculation,
            'probability_origin':'deterministic_from_explicit_model_parameters',
            'factual_review_pending':True,'publication':False}
