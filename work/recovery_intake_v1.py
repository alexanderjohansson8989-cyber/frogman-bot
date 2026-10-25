"""Bounded recovery gate for a future runner, never forecast/model POSTs.

No scheduling or automatic activation here. A runner must call this BEFORE
question intake and proceed only on ready=True. Unresolved effects stay in
the journal for readback/operator inspection; no resending or SQL repair.
"""
import datetime as dt
from candidate_recovery_v1 import RecoveryHost,inspect
from operations_ledger_v1 import GlobalStop

class ReadOnlyRecoveryClient:
    def __init__(self,client,host,clock,*,seconds=90,max_gets=10,total_seconds=300):
        self.client,self.host,self.clock=client,host,clock
        self.seconds=seconds;self.deadline=None
        self.total_deadline=clock()+dt.timedelta(seconds=total_seconds)
        self.max_gets,self.gets=max_gets,0
    def guard(self):
        deadline=min(self.total_deadline,self.deadline or self.total_deadline)
        if self.host.closed or self.clock()>=deadline:
            raise GlobalStop('Recovery read window expired')
        self.host.store.assert_owner(self.host.owner)
        if self.clock()>=deadline:raise GlobalStop('Recovery read window expired')
    def call(self,callback,*args):
        if self.deadline is None:self.deadline=self.clock()+dt.timedelta(seconds=self.seconds)
        self.guard()
        if self.gets>=self.max_gets:raise GlobalStop('Recovery GET limit')
        self.gets+=1
        value=callback(*args)
        self.guard()
        return value
    def fetch(self,post_id):return self.call(self.client.me),self.call(self.client.post,post_id)
    def comments(self,*args):return self.call(self.client.comments,*args)

def recover_before_intake(store,owner,folder,*,client,get_run,clock):
    """At most one recovery pass; ready=False must stop new paid/write work."""
    state=inspect(store)
    if state['owner'] is None:
        return dict(ready=not state['blocked'],status='clean' if not state['blocked'] else 'operator_required',
                    reason=None if not state['blocked'] else 'Ownerless blocked journal',gets=0)
    if state['owner']==owner:
        return dict(ready=False,status='busy',reason='Current owner already holds journal',gets=0)
    old=state['owner'];run=get_run(old['run_id'])
    if (run.get('id')!=int(old['run_id']) or run.get('run_attempt')!=old['run_attempt']
            or run.get('repository',{}).get('full_name')!=store.repository):
        raise GlobalStop('Recovery workflow identity differs')
    if run.get('status')!='completed':
        return dict(ready=False,status='busy',reason='Previous owning workflow is not terminal',gets=0)
    # At most one forecast and its comment can be uncertain in normal serial
    # operation. Refuse a corrupt/unexpected backlog instead of expanding work.
    if len([s for s in state['submissions'] if s['state']=='uncertain'])>2:
        return dict(ready=False,status='operator_required',reason='Unexpected uncertain submission backlog',gets=0)
    h=RecoveryHost(store,owner,folder,get_run=lambda _:run,clock=clock)
    reader=ReadOnlyRecoveryClient(client,h,clock)
    result=dict(ready=False,status='operator_required',steps={},gets=0)
    try:
        result['steps']['unsent_models']=h.cancel_proven_unsent()
        reader.guard()
        result['steps']['unsent_submissions']=h.cancel_proven_unsent_submissions()
        reader.guard()
        result['steps']['settle']=h.settle_saved()
        reader.guard()
        result['steps']['forecasts']=h.readback_forecasts(reader.fetch)
        result['steps']['comments']=h.readback_comments(reader)
        reader.guard()
        h.abandon_finished_work()
        # An interrupted question stays attempted. Only NEW intake may follow.
        result.update(ready=True,status='recovered')
    except GlobalStop as exc:
        result['reason']=str(exc)[:180]
    finally:
        result['gets']=reader.gets
        try:h.close()
        except GlobalStop as exc:
            result.update(ready=False,status='operator_required',reason=str(exc)[:180])
    if result['ready']:
        after=inspect(store)
        if after['owner'] is not None or after['blocked']:
            raise GlobalStop('Recovery release was not verified')
    return result
