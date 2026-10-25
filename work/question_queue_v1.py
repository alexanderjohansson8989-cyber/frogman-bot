"""Bounded serial preparation queue. No scheduler, submission or default I/O.

The caller supplies authenticated full posts and a fresh GET callback. Timing
margin is an explicit estimate, not a promise that a model call will finish.
Completed attempts (including abstentions) are never rerun automatically.
"""
import copy
import datetime as dt
import math
from bounded_evidence import digest
from candidate_service_v1 import whole_question_reserve
from competition_adapter_v1 import bind_post, identity, positive_id
from general_evaluation import instant
from independent_runner import Stop
from operations_ledger_v1 import GlobalStop, units
from outcome_metrics_v1 import queue_metrics

VERSION='serial-preparation-queue-v1'

def session_key(b):
    return f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"

def run_queue(posts, me, ledger, *, tournament_id, expected_username, clock,
              job_deadline_utc, minimum_runtime_seconds, max_questions,
              refresh_post, run_binding, guard_host=lambda:None):
    """Prepare at most max_questions; injected run_binding must finish a session.

    Deadline order uses the input snapshot. A refreshed contract/deadline change
    is skipped until a new intake, rather than generating on a stale snapshot.
    Fetch errors skip one question; GlobalStop always stops the whole queue.
    """
    if (not isinstance(posts,list) or len(posts)>100 or type(max_questions) is not int
        or not 1<=max_questions<=10 or type(minimum_runtime_seconds) not in (int,float)
        or not math.isfinite(minimum_runtime_seconds) or minimum_runtime_seconds<=0):
        raise ValueError('Bounded intake, attempts and positive timing estimate required')
    identity(me,expected_username);positive_id(tournament_id)
    deadline=instant(job_deadline_utc)
    margin=dt.timedelta(seconds=minimum_runtime_seconds)
    out=dict(version=VERSION,publication=False,attempted=[],skipped=[],stop_reason=None,
             minimum_runtime_seconds=minimum_runtime_seconds,job_deadline_utc=deadline.isoformat())
    def guard():
        ledger.guard()
        if ledger.db.execute("SELECT 1 FROM sessions WHERE status='active'").fetchone():
            raise GlobalStop('Active session must be reconciled before queue intake')
        guard_host()
    def skip(post_id,reason):out['skipped'].append(dict(post_id=post_id,reason=reason))
    def attempted(b):
        return ledger.db.execute('SELECT 1 FROM sessions WHERE key=?',(session_key(b),)).fetchone() is not None
    def bind(p):
        # Never accept a pre-bound diagnostic with a substituted spot deadline.
        return bind_post(p,me,tournament_id=tournament_id,expected_username=expected_username,now=clock())
    guard();pending=[];seen={}
    for p in copy.deepcopy(posts):
        pid=p.get('id') if isinstance(p,dict) else None
        try:b=bind(p)
        except (Stop,ValueError,TypeError,KeyError,AttributeError):
            skip(pid,'ineligible_snapshot');continue
        key=session_key(b);fingerprint=digest(p)
        if key in seen:
            if seen[key]!=fingerprint:raise GlobalStop('Conflicting duplicate question snapshots')
            skip(pid,'duplicate_intake');continue
        seen[key]=fingerprint
        if attempted(b):skip(pid,'already_attempted');continue
        pending.append(b)
    pending.sort(key=lambda b:(instant(b['cutoff_utc']),b['question_id'],b['post_id']))
    for original in pending:
        guard()
        now=clock();pid=original['post_id']
        if len(out['attempted'])>=max_questions:out['stop_reason']='attempt_limit';break
        if now+margin>=deadline:out['stop_reason']='job_time_margin';break
        if now+margin>=instant(original['cutoff_utc']):skip(pid,'question_time_margin');continue
        if attempted(original):skip(pid,'already_attempted');continue
        if ledger.available_units()<units(whole_question_reserve()):
            out['stop_reason']='complete_question_budget';break
        # Read-only refresh immediately before spending. No automatic retry.
        try:fresh=refresh_post(pid)
        except GlobalStop:raise
        except Exception:skip(pid,'refresh_failed');continue
        guard()
        try:b=bind(fresh)
        except (Stop,ValueError,TypeError,KeyError,AttributeError):
            skip(pid,'ineligible_refresh');continue
        if any(b[k]!=original[k] for k in ('post_id','question_id','question_contract_sha256','cutoff_utc')):
            skip(pid,'snapshot_changed');continue
        if clock()+margin>=min(deadline,instant(b['cutoff_utc'])):
            skip(pid,'time_margin_after_refresh');continue
        guard()
        result=run_binding(copy.deepcopy(b))
        out['attempted'].append(dict(post_id=pid,question_id=b['question_id'],
            binding_sha256=digest(b),status=result['status']))
        if 'outcome_metrics' in result:
            out['attempted'][-1]['outcome_metrics']=copy.deepcopy(result['outcome_metrics'])
        if result['status']=='global_stop':out['stop_reason']='global_stop';break
        row=ledger.db.execute('SELECT status FROM sessions WHERE key=?',(session_key(b),)).fetchone()
        if row!=('complete',):raise GlobalStop('Queue executor did not durably finish the question')
    out['outcome_metrics']=queue_metrics(out,intake_records=len(posts),eligible_new_questions=len(pending))
    return out

def run_host_queue(host,posts,me,*,tournament_id,expected_username,job_deadline_utc,
                   minimum_runtime_seconds,max_questions,refresh_post,**run_kwargs):
    """Integrate with DurableHost; its checks still precede every model effect.

    The job deadline only tightens host expiry. Resetting the previous question's
    deadline is allowed only between completed sessions, never during recovery.
    """
    deadline=min(host.expiry,instant(job_deadline_utc))
    host.expiry=deadline
    def guard():
        if host.ledger.db.execute("SELECT 1 FROM sessions WHERE status='active'").fetchone():
            raise GlobalStop('Cannot advance host with active session')
        host.effect_deadline=deadline
        host.guard_local()
    def refresh(pid):
        host.before_effect()
        value=refresh_post(pid)
        host.guard_local()
        return value
    return run_queue(posts,me,host.ledger,tournament_id=tournament_id,expected_username=expected_username,
        clock=host.clock,job_deadline_utc=deadline.isoformat(),minimum_runtime_seconds=minimum_runtime_seconds,
        max_questions=max_questions,refresh_post=refresh,
        run_binding=lambda b:host.run(b,**run_kwargs),guard_host=guard)
