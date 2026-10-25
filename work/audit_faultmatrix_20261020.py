"""Offline fault matrix over every Git boundary of one real-client model dispatch."""
import json,sqlite3,tempfile
from pathlib import Path
from unittest.mock import patch
from test_git_state_store_v1 import FakeGit,bootstrap,store
from test_candidate_runtime_v1 import new_host,FakeHTTP,raw,need
from candidate_api_v1 import ClaudeClient
from candidate_dispatch_v1 import model_transport
from operations_ledger_v1 import GlobalStop,ReservedRequester

METHODS=('repository','head','commit','tree','blob','create_blob','create_tree','create_commit','update_ref')
def scenario(fail_at=None,after=False):
    git=FakeGit();bootstrap(git);trace=[];mode={'armed':True}
    with tempfile.TemporaryDirectory() as tmp:
        host=new_host(git,tmp)
        class CheckedHTTP(FakeHTTP):
            def open(self,req,timeout):
                armed=mode['armed'];mode['armed']=False
                try:
                    files=store(git).load()[1];db=sqlite3.connect(':memory:')
                    try:
                        db.deserialize(files['journal.sqlite3'])
                        assert db.execute('SELECT state FROM operations').fetchall()==[('uncertain',)]
                        assert db.execute('SELECT kind FROM effect_permits').fetchall()==[('model',)]
                    finally:db.close()
                finally:mode['armed']=armed
                return super().open(req,timeout)
        net=CheckedHTTP([raw()])
        for name in METHODS:
            method=getattr(git,name)
            def wrapped(*args,_method=method,_name=name,**kw):
                if not mode['armed']:return _method(*args,**kw)
                trace.append(_name);hit=len(trace)==fail_at
                if hit and not after:raise GlobalStop('Synthetic Git boundary failure')
                value=_method(*args,**kw)
                if hit:raise GlobalStop('Synthetic lost Git acknowledgement')
                return value
            setattr(git,name,wrapped)
        request=ReservedRequester(host.ledger,'q',transport=model_transport(host,ClaudeClient('synthetic',opener=net)),count=lambda _:100,guard=host.before_effect)
        error=None
        with patch('git_state_store_v1.time.sleep'):
            try:request('need',need())
            except GlobalStop as e:error=type(e).__name__
        count=len(trace);mode['armed']=False
        # A second caller attempt must never duplicate an already sent effect.
        try:request('need',need())
        except GlobalStop:pass
        assert len(net.calls)<=1
        state=host.ledger.db.execute('SELECT state FROM operations').fetchall()
        try:host.close()
        except GlobalStop:pass
        return dict(fail_at=fail_at,after=after,error=error,git_calls=count,model_writes=len(net.calls),state=state,trace=trace)

def run():
    baseline=scenario();cases=[]
    for n in range(1,baseline['git_calls']+1):
        for after in (False,True):cases.append(scenario(n,after))
    return dict(baseline=baseline,cases=cases,case_count=len(cases),all_single_dispatch=True,remote_uncertain_and_permit_before_every_dispatch=True,network_used=False)

if __name__=='__main__':
    result=run();folder=Path('outputs/korningar/broad_audit_20261020');folder.mkdir(parents=True,exist_ok=True)
    (folder/'fault_matrix.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('cases','baseline')}))
