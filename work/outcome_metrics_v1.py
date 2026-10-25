"""Descriptive counters only. Never affect eligibility, retries or publication."""
from collections import Counter

VERSION='outcome-metrics-v1'


def question_metrics(result):
    answer=result.get('raw_answer') or {}
    review=result.get('review') or {}
    raw_forecast=answer.get('decision')=='forecast'
    decision=review.get('decision','not_reached')
    if decision not in ('pass','qualified','unavailable','reject','not_reached'):decision='unknown'
    labels=review.get('labels',[])
    captures=[result[k] for k in ('initial_capture','child_capture') if k in result]
    attempts=[c.get('source_attempts') for c in captures]
    # Some old/injected captures lack attempt counts; do not fabricate them.
    attempts_known=bool(captures) and all(type(n) is int and n>=0 for n in attempts)
    stage_counts=Counter(d.get('stage','unknown') for d in result.get('diagnostics',[]))
    errors=Counter(d.get('error_type','unknown') for d in result.get('diagnostics',[]))
    preparation=('prepared' if result.get('submission_plan') is not None else
                 'failed' if result.get('status')=='preparation_stop' else 'not_reached')
    return dict(version=VERSION,observation_scope='service_return_snapshot',
        raw_forecast=raw_forecast,model_abstained=answer.get('decision')=='abstain',
        service_status=result.get('status','unknown'),review_decision=decision,
        review_eligibility=review.get('eligibility','not_reached'),
        review_hold=raw_forecast and review.get('eligibility')=='hold',
        review_labels=sorted(set(labels)),
        review_technical_error_count=len(review.get('technical_errors',[])),
        preparation=preparation,capture_reached=bool(captures),
        captured_document_records=len(result.get('documents',[])),
        capture_failure_records=sum(len(c.get('failures',[])) for c in captures),
        source_attempts=sum(attempts) if attempts_known else None,
        diagnostic_stages=dict(sorted(stage_counts.items())),
        diagnostic_error_types=dict(sorted(errors.items())),
        requester_stages=[s.get('stage','unknown') for s in result.get('stage_timings',[])])


def queue_metrics(queue, *, intake_records, eligible_new_questions):
    attempted=queue['attempted']
    known=[a['outcome_metrics'] for a in attempted
           if a.get('outcome_metrics',{}).get('version')==VERSION]
    forecasts=[m for m in known if m['raw_forecast']]
    holds=sum(m['review_hold'] for m in forecasts)
    def count(key,rows):return dict(sorted(Counter(m[key] for m in rows).items()))
    return dict(version=VERSION,scope='this_intake_only_not_season_coverage',
        intake_records=intake_records,eligible_new_questions=eligible_new_questions,
        attempted_questions=len(attempted),attempts_with_metrics=len(known),
        attempts_without_metrics=len(attempted)-len(known),raw_forecasts=len(forecasts),
        model_abstentions=sum(m['model_abstained'] for m in known),
        review_decisions_among_raw_forecasts=count('review_decision',forecasts),
        held_raw_forecasts=holds,
        hold_fraction_among_raw_forecasts=holds/len(forecasts) if forecasts else None,
        hold_labels=dict(sorted(Counter(label for m in forecasts if m['review_hold'] for label in m['review_labels']).items())),
        preparation_outcomes=count('preparation',known),
        final_attempt_statuses=count('status',attempted),
        skipped_records_by_reason=count('reason',queue['skipped']),stop_reason=queue['stop_reason'])
