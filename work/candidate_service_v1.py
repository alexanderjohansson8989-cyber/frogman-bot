"""Integrated one-question dry run with persistent budget/session ownership.

There is no default transport and no live submission entry point. This is the
shared core for a later explicitly configured deployment host.
"""
import copy,datetime as dt
from decimal import Decimal
from advisor_policy import OPUS,RATES
from bounded_evidence import digest
from general_evaluation import instant
from candidate_pipeline_v1 import run_question
from candidate_profile_v1 import OUT,STAGE_LIMITS
from competition_adapter_v1 import prepare
from operations_ledger_v1 import ReservedRequester,GlobalStop
from independent_runner import Stop
from outcome_metrics_v1 import question_metrics

VERSION='candidate-service-v1-dry-run'
def whole_question_reserve():
    a,b=RATES[OPUS]
    return Decimal('1.20')+sum((Decimal(36000)*a+Decimal(OUT[k])*b)*STAGE_LIMITS[k]/1000000 for k in ('need','forecast','review'))

def run_bound_question(binding,folder,ledger,*,transport,count,clock,save,capture_initial,capture_follow,candidates,guard_host=lambda:None,publish=False):
    b=copy.deepcopy(binding);start=clock();cutoff=min(instant(b['cutoff_utc']),start+dt.timedelta(hours=1))
    def guard():
        guard_host()
        if clock()>=cutoff:raise GlobalStop('Question time window passed')
    guard();key=f"{b['actor']['id']}/{b['tournament_id']}/{b['question_id']}"
    ledger.start_session(key,b,whole_question_reserve())
    requester=ReservedRequester(ledger,key,transport=transport,count=count,guard=guard)
    result=run_question(b['question'],folder,request=requester,count=count,clock=clock,save=save,
        capture_initial=capture_initial,capture_follow=capture_follow,candidates=candidates,guard=guard)
    result['service_version']=VERSION;result['binding_sha256']=digest(b)
    if result['status']=='global_stop':
        result['outcome_metrics']=question_metrics(result);save(copy.deepcopy(result));return result
    if result['status']=='candidate':
        try:result['submission_plan']=prepare(b,result);result['status']='prepared_local_only'
        except (Stop,ValueError) as exc:
            result['status']='preparation_stop';result['diagnostics'].append(dict(stage='prepare',error_type=type(exc).__name__,reason=str(exc)[:160]))
    result['outcome_metrics']=question_metrics(result)
    save(copy.deepcopy(result));ledger.finish_session(key,result,publish)
    return result
