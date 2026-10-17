"""Offline reference -> proposal -> existing audit bridge; never calls an API.

Reference eligibility is a separate caller-supplied review, not inferred from
successful arithmetic or from the candidate's auditor. Hashes bind exact inputs;
they establish integrity, not truth. Live orchestration remains separate.
"""
import copy
import json
from bounded_evidence import digest, forecast_payload, text_answer_check
from bounded_pipeline import audit_documents
from data_forecast_candidate import count_payload, resolve_count_reply
from update_review import payload as review_payload, check as review_check
from independent_runner import Stop


def _seal(body):
    body = copy.deepcopy(body)
    return {'body': body, 'sha256': digest(body)}


def _read(record):
    if set(record) != {'body', 'sha256'} or digest(record['body']) != record['sha256']:
        raise Stop('Reference bridge record changed')
    return copy.deepcopy(record['body'])


def prepare(question, reference_answer, reference_documents, new_documents,
            research_time, *, method, eligibility, count_target=None, audit_mode='information_update',
            verified_computations=None):
    """Preserve baseline/source distinction before full token preflight.

    eligibility = {status: eligible|unverified|invalid, reason: ..., review_id: ...}
    is a host decision. It MUST NOT be produced by the proposed-change auditor.
    """
    if audit_mode not in ('information_update','same_data_method'):
        raise Stop('Explicit reference audit mode')
    if audit_mode == 'information_update' and verified_computations:
        raise Stop('Calculation receipts require separate method mode')
    if (set(eligibility) != {'status', 'reason', 'review_id'} or
            eligibility['status'] not in ('eligible', 'unverified', 'invalid') or
            any(not isinstance(eligibility[k], str) or not eligibility[k].strip()
                for k in ('reason', 'review_id'))):
        raise Stop('Explicit independent reference eligibility required')
    if (set(method) != {'id', 'artifact_sha256', 'assumptions', 'limitations'} or
            not isinstance(method['id'], str) or not method['id'].strip() or
            not isinstance(method['artifact_sha256'], str) or
            len(method['artifact_sha256']) != 64 or
            any(c not in '0123456789abcdef' for c in method['artifact_sha256']) or
            any(not isinstance(method[k], list) or not method[k] or
                any(not isinstance(x, str) or not x.strip() for x in method[k])
                for k in ('assumptions', 'limitations'))):
        raise Stop('Reference method and limitations required')
    prior = forecast_payload(question, reference_documents, research_time, 12000)
    raw = {'content': [{'type': 'text', 'text': '<forecast>' +
            json.dumps(reference_answer, allow_nan=False) + '</forecast>'}]}
    baseline = text_answer_check(raw, question, prior)
    if baseline['decision'] != 'forecast':
        raise Stop('Reference distribution required')
    docs = reference_documents + new_documents
    request = (count_payload(question, docs, research_time, count_target) if count_target
               else forecast_payload(question, docs, research_time, 12000))
    body = json.loads(request['messages'][0]['content'])
    body['computed_reference'] = dict(answer=baseline, method=method, eligibility=eligibility)
    request['messages'][0]['content'] = json.dumps(body, ensure_ascii=False, allow_nan=False)
    request['system'] += '''\nThe computed_reference is a host-calculated development
reference, not ground truth. Its method assumptions and limitations are explicit.
Evaluate source/target compatibility. Keep observed facts separate from subjective
adjustments. Explain any difference from the reference using supplied evidence;
do not invent historical rates. Reference eligibility is not yours to authorize.
You may abstain. Your proposal and the reference will remain separate records.'''
    return _seal(dict(version='reference-bridge-1', question=question, baseline=baseline,
        method=method, eligibility=eligibility, prior_documents=audit_documents(prior),
        request=request, count_target=count_target, audit_mode=audit_mode,
        verified_computations=verified_computations or [], publication=False))


def propose(prepared, reply, actual_request):
    """Only accept the exact request that was prepared and token-counted by caller."""
    state = _read(prepared)
    if actual_request != state['request']:
        raise Stop('Sent request differs; rebuild reference bridge before preflight')
    if state['count_target']:
        resolved = resolve_count_reply(reply, state['question'], actual_request, state['count_target'])
        candidate = resolved['answer']
    else:
        candidate = text_answer_check(reply, state['question'], actual_request)
        resolved = None
    pack = dict(question=state['question'], baseline=state['baseline'], candidate=candidate,
        prior_documents=state['prior_documents'], new_documents=audit_documents(actual_request),
        reference_method=state['method'], reference_eligibility=state['eligibility'])
    mode = state.get('audit_mode','information_update')
    if mode == 'same_data_method': pack['verified_computations']=state['verified_computations']
    changed = candidate['distribution'] != state['baseline']['distribution']
    needs_audit = candidate['decision'] == 'forecast' and changed
    if mode == 'same_data_method':
        from method_review import payload as method_payload
        audit_request = method_payload(pack) if needs_audit else None
    else:
        audit_request = review_payload(pack) if needs_audit else None
    if audit_request:
        audit_request['system'] += '''\nAlso inspect reference_method and reference_eligibility.
An invalid or unverified reference cannot supply an essential factual premise for
accepting the candidate. Check the candidate on its own supplied factual support.
Your rejection never establishes reference eligibility or authorizes fallback.'''
    return _seal(dict(prepared_sha256=prepared['sha256'], state=state, candidate=candidate,
        raw_response_sha256=digest(reply), computed_candidate=resolved, pack=pack,
        audit_request=audit_request))


def select(proposal, audit_reply=None, actual_audit_request=None):
    state = _read(proposal)
    candidate = state['candidate']
    eligibility = state['state']['eligibility']['status']
    selected = None
    selection = 'abstain'
    review = None
    if state['audit_request'] is not None:
        if audit_reply is None or actual_audit_request != state['audit_request']:
            raise Stop('Exact completed audit required for changed proposal')
        if state['state'].get('audit_mode') == 'same_data_method':
            from method_review import check as method_check
            review = method_check(audit_reply, state['pack'])
        else:
            review = review_check(audit_reply, state['pack'])
        if review['selection'] == 'candidate':
            selected, selection = candidate, 'candidate'
        elif eligibility == 'eligible':
            selected, selection = state['state']['baseline'], 'reference'
    else:
        if audit_reply is not None or actual_audit_request is not None:
            raise Stop('Unexpected audit for unchanged or abstained proposal')
        # Abstention must not be silently turned into a forecast. Equality with
        # a baseline is also not a factual endorsement of that baseline.
        if candidate['decision'] == 'forecast' and eligibility == 'eligible':
            selected, selection = state['state']['baseline'], 'reference'
    return _seal(dict(version='reference-selection-1', proposal_sha256=proposal['sha256'],
        selection=selection, selected_answer=selected, reference=state['state']['baseline'],
        candidate=candidate, review=review,
        audit_response_sha256=digest(audit_reply) if audit_reply is not None else None,
        semantic_review_pending=True, future_accuracy_verified=False, publication=False))
