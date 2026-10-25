"""Disabled, injected one-cycle production core. No scheduler or credentials.

Existing private state must be initialized separately. Activation, listing,
quota observation and source capture are supplied by a later audited runner.
"""
import datetime as dt
from decimal import Decimal
from pathlib import Path
from candidate_dispatch_v1 import model_transport
from candidate_service_v1 import whole_question_reserve
from competition_adapter_v1 import identity
from durable_candidate_host_v1 import DurableHost
from operations_ledger_v1 import GlobalStop,units
from question_queue_v1 import run_queue
from recovery_intake_v1 import recover_before_intake
from general_evaluation import instant
from publication_intent_v1 import pending,load,advance,key_for
from question_archive_v1 import compact_completed

def validate(config,store,clock):
    now=clock()
    if config.get('version')!='production-cycle-v1':raise GlobalStop('Explicit production configuration required')
    if type(config.get('submit')) is not bool:raise GlobalStop('Explicit submission mode required')
    if type(config.get('max_questions')) is not int or not 1<=config['max_questions']<=2:
        raise GlobalStop('Small bounded cycle required')
    if type(config.get('actor_id')) is not int or config['actor_id']<=0:raise GlobalStop('Expected actor required')
    if type(config.get('tournament_id')) is not int or config['tournament_id']<=0:raise GlobalStop('Expected tournament required')
    if not isinstance(config.get('username'),str) or not config['username']:raise GlobalStop('Expected username required')
    cap=units(config['cap_usd']);balance=units(config['console_balance_usd'])
    if cap!=store.cap or config['period']!=store.period or balance<cap or cap<units(whole_question_reserve()):
        raise GlobalStop('Credit allocation and state policy differ')
    observed=instant(config['console_observed_utc'])
    if not observed<=now<observed+dt.timedelta(hours=1):raise GlobalStop('Stale shared-credit observation')
    deadline=min(instant(config[k]) for k in ('credit_expires_utc','key_expires_utc','job_deadline_utc'))
    if not now<deadline<=now+dt.timedelta(hours=1):raise GlobalStop('Bounded valid cycle window required')
    margin=config.get('minimum_runtime_seconds')
    if type(margin) is not int or not 60<=margin<=3600:raise GlobalStop('Explicit measured timing margin required')
    # This is a configured reserve, not a claimed measurement of future cost.
    if type(config.get('git_reserve')) is not int or config['git_reserve']<150:
        raise GlobalStop('Explicit Git capacity reserve required')
    if ('pending_limit' in config and (type(config['pending_limit']) is not int or not 1<=config['pending_limit']<=2)
            or 'resume_only_when_pending' in config and type(config['resume_only_when_pending']) is not bool):
        raise GlobalStop('Explicit bounded continuation policy required')
    return deadline

def run_cycle(config,store,owner,folder,*,mc,claude,listing,get_run,quota,clock,
              capture_initial,capture_follow,candidates,enabled=False,capture_factory=None,before_intake=None):
    if enabled is not True:raise GlobalStop('Production cycle disabled')
    deadline=validate(config,store,clock)
    def check_quota():
        q=quota();now=clock()
        if (type(q.get('remaining')) is not int or q['remaining']<config['git_reserve']
            or not instant(q['observed_utc'])<=now<instant(q['observed_utc'])+dt.timedelta(seconds=60)
            or now>=instant(q['reset_utc'])):raise GlobalStop('Git quota unknown, stale or insufficient')
    check_quota()
    recovered=recover_before_intake(store,owner,Path(folder)/'recovery',client=mc,get_run=get_run,clock=clock)
    out=dict(version='production-cycle-result-v1',recovery=recovered,publication=config['submit'],queue=None)
    if recovered['ready'] is not True:return out
    class CycleHost(DurableHost):
        def before_effect(self):
            super().before_effect();check_quota()
    host=CycleHost(store,owner,Path(folder)/'host',clock=clock,
        credit_expires_utc=config['credit_expires_utc'],key_expires_utc=config['key_expires_utc'])
    host.expiry=min(host.expiry,deadline)
    def guard():
        host.effect_deadline=host.expiry
        host.before_effect()
    def read(callback,*a):guard();value=callback(*a);guard();return value
    try:
        if before_intake is not None:before_intake(host)
        if capture_factory is not None:
            capture_initial,capture_follow=capture_factory(host)
        me=read(mc.me)
        if identity(me,config['username'])['id']!=config['actor_id']:raise GlobalStop('Production account differs')
        out['continued']=[]
        keys=pending(host.ledger)
        if keys and not config['submit']:raise GlobalStop('Pending publication requires explicit submission mode')
        if len(keys)>config.get('pending_limit',2):raise GlobalStop('Unexpected publication backlog')
        for key in keys:
            plan=load(host.ledger,key);b=plan['binding']
            if b['actor']!=identity(me,config['username']) or b['tournament_id']!=config['tournament_id']:
                raise GlobalStop('Pending publication account/tournament differs')
            out['continued'].append(advance(host,mc,key,enabled=True))
        compact_completed(host)
        posts=[] if out['continued'] and config.get('resume_only_when_pending') else read(listing,config['tournament_id'])
        # Every question rechecks model access before paid requests.
        def execute(b):
            host.before_effect()
            from advisor_policy import OPUS
            if claude.metadata().get('id')!=OPUS:raise GlobalStop('Production model access differs')
            result=host.run(b,transport=model_transport(host,claude),count=claude.count,
                capture_initial=capture_initial,capture_follow=capture_follow,candidates=candidates,publish=config['submit'])
            if result['status']=='prepared_local_only' and config['submit']:
                plan=result['submission_plan']
                service_metrics=result.get('outcome_metrics')
                result=advance(host,mc,key_for(plan),enabled=True)
                if service_metrics is not None:result['outcome_metrics']=service_metrics
            if result['status']!='global_stop':compact_completed(host)
            return result
        out['queue']=run_queue(posts,me,host.ledger,tournament_id=config['tournament_id'],
            expected_username=config['username'],clock=clock,job_deadline_utc=deadline.isoformat(),
            minimum_runtime_seconds=config['minimum_runtime_seconds'],max_questions=config['max_questions'],
            refresh_post=lambda pid:read(mc.post,pid),run_binding=execute,guard_host=guard)
    finally:host.close()
    out.update(owner_released=store.manifest['owner'] is None,state_head=store.head)
    return out
