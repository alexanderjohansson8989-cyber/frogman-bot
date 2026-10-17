"""Bounded fixed-origin GitHub Git-data transport. No model/forecast APIs."""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from git_state_store_v1 import MAX_FILE, valid_sha, blob_sha
from operations_ledger_v1 import GlobalStop

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise GlobalStop('GitHub state redirect forbidden')

class GitHubGit:
    def __init__(self, repository, token, *, timeout=20):
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository)
                or not isinstance(token,str) or not token.strip() or not 1<=timeout<=30):
            raise GlobalStop('Explicit repository and credential required')
        self.repo=repository;self._token=token;self.timeout=timeout
        self.base='https://api.github.com/repos/'+repository
        self.opener=urllib.request.build_opener(NoRedirect())

    def _call(self,method,path='',body=None,missing=False):
        run_read=method=='GET' and re.fullmatch(r'/actions/runs/[1-9][0-9]*',path)
        if method not in ('GET','POST','PATCH') or not (path=='' or path.startswith('/git/') or run_read):
            raise GlobalStop('Unapproved state endpoint')
        raw=None if body is None else json.dumps(body,allow_nan=False).encode()
        req=urllib.request.Request(self.base+path,data=raw,method=method,headers={
            'Authorization':'Bearer '+self._token,'Accept':'application/vnd.github+json',
            'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'Frogman-state-v1'})
        try:
            with self.opener.open(req,timeout=self.timeout) as response:
                data=response.read(2*MAX_FILE+1)
                if len(data)>2*MAX_FILE:raise GlobalStop('GitHub state response too large')
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            if missing and exc.code==404:return None
            raise GlobalStop('GitHub state HTTP '+str(exc.code)) from None
        except GlobalStop:raise
        except Exception:
            # Never expose response bodies, request headers or credential text.
            raise GlobalStop('GitHub state transport outcome unknown') from None

    def repository(self):return self._call('GET')
    def workflow_run(self,run_id):
        if not isinstance(run_id,str) or not re.fullmatch(r'[1-9][0-9]*',run_id):raise GlobalStop('Workflow run identity')
        return self._call('GET','/actions/runs/'+run_id)
    def _branch(self,branch):
        if not re.fullmatch(r'frogman-state-[a-z0-9-]{1,60}',branch):raise GlobalStop('State branch only')
        return urllib.parse.quote(branch,safe='')
    def head(self,branch):
        r=self._call('GET','/git/ref/heads/'+self._branch(branch),missing=True)
        if r is None:return None
        if r.get('ref')!='refs/heads/'+branch or r.get('object',{}).get('type')!='commit':raise GlobalStop('State reference mismatch')
        return valid_sha(r['object']['sha'])
    def commit(self,sha):return self._call('GET','/git/commits/'+valid_sha(sha))
    def tree(self,sha):
        r=self._call('GET','/git/trees/'+valid_sha(sha))
        if r.get('truncated') is not False:raise GlobalStop('Truncated state tree')
        return r['tree']
    def blob(self,sha):
        r=self._call('GET','/git/blobs/'+valid_sha(sha))
        if r.get('encoding')!='base64' or not 0<r.get('size',0)<=MAX_FILE:raise GlobalStop('State blob size/encoding')
        try:data=base64.b64decode(''.join(r['content'].split()),validate=True)
        except Exception:raise GlobalStop('State blob decoding failed') from None
        if len(data)!=r['size'] or blob_sha(data)!=sha:raise GlobalStop('State blob content mismatch')
        return data
    def create_blob(self,data):
        if not isinstance(data,bytes) or not 0<len(data)<=MAX_FILE:raise GlobalStop('State blob size')
        return valid_sha(self._call('POST','/git/blobs',dict(content=base64.b64encode(data).decode(),encoding='base64'))['sha'])
    def create_tree(self,entries):return valid_sha(self._call('POST','/git/trees',dict(tree=entries))['sha'])
    def create_commit(self,tree,parents):
        if len(parents)>1:raise GlobalStop('No merged state commits')
        return valid_sha(self._call('POST','/git/commits',dict(message='Frogman state checkpoint',tree=valid_sha(tree),parents=[valid_sha(p) for p in parents]))['sha'])
    def create_ref(self,branch,sha):
        return self._call('POST','/git/refs',dict(ref='refs/heads/'+self._branch(branch),sha=valid_sha(sha)))
    def update_ref(self,branch,sha,*,force=False):
        if force is not False:raise GlobalStop('Force update forbidden')
        return self._call('PATCH','/git/refs/heads/'+self._branch(branch),dict(sha=valid_sha(sha),force=False))
