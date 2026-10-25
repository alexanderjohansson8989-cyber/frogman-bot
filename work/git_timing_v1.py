"""Opt-in elapsed-time telemetry; no credentials/arguments/results in timing events.

When used with GitHubGit, events measure actual
elapsed I/O. FakeGit only tests instrumentation; it cannot measure network.
"""
from time import perf_counter
from git_state_store_v1 import SnapshotStore

class TimedGit:
    def __init__(self,inner,events,clock=perf_counter):self.inner,self.events,self.clock=inner,events,clock
    def __getattr__(self,name):
        value=getattr(self.inner,name)
        if not callable(value):return value
        def call(*args,**kwargs):
            start=self.clock();error=None
            try:return value(*args,**kwargs)
            except BaseException as exc:error=type(exc).__name__;raise
            finally:self.events.append(dict(kind='git-call',method=name,seconds=max(0,self.clock()-start),error_type=error))
        return call

class TimedSnapshotStore(SnapshotStore):
    def __init__(self,*args,timing_events,timing_clock=perf_counter,**kwargs):
        self.timing_events,self.timing_clock=timing_events,timing_clock
        super().__init__(*args,**kwargs)
    def _write(self,files,owner,bootstrap=False):
        start=self.timing_clock();error=None
        try:return super()._write(files,owner,bootstrap=bootstrap)
        except BaseException as exc:error=type(exc).__name__;raise
        finally:self.timing_events.append(dict(kind='snapshot-write',seconds=max(0,self.timing_clock()-start),
                file_count=len(files),snapshot_bytes=sum(len(v) for v in files.values()),bootstrap=bootstrap,error_type=error))
