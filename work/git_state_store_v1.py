"""Private Git snapshots with one-parent fast-forward checkpoints.

All Git I/O is injected. A lost write acknowledgement permits readback only.
No automatic bootstrap, lease stealing, state merge or external API retry.
"""
import copy
import hashlib
import json
import re
import time
from operations_ledger_v1 import GlobalStop
from general_evaluation import instant

VERSION = 'frogman-git-state-v1'
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILES = 256
SHA = re.compile(r'[0-9a-f]{40}')
NAME = re.compile(r'(?:journal\.sqlite3|a-[0-9a-f]{64}\.(?:json|bin))')

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def blob_sha(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()

def valid_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise GlobalStop('Invalid Git object identity')
    return value

def owner_id(run_id, attempt, nonce):
    if (not isinstance(run_id, str) or not run_id.isdigit() or not run_id.strip('0')
            or type(attempt) is not int or attempt != 1 or not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{32}', nonce)):
        raise GlobalStop('First-attempt run identity required')
    return dict(run_id=run_id, run_attempt=attempt, nonce=nonce)

def validate_files(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES or 'journal.sqlite3' not in files:
        raise GlobalStop('State file inventory limit')
    total = 0
    for name, data in files.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(data, bytes) or not 0 < len(data) <= MAX_FILE:
            raise GlobalStop('State file path/size')
        if name.startswith('a-') and name[2:66] != sha256(data):
            raise GlobalStop('Archive content identity')
        total += len(data)
    if total > MAX_TOTAL:
        raise GlobalStop('State snapshot size limit')


class SnapshotStore:
    def __init__(self, git, *, repository, branch, period, cap_units, code_sha256, clock):
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
                or not re.fullmatch(r'frogman-state-[a-z0-9-]{1,60}', branch)
                or not isinstance(period, str) or not period.strip()
                or type(cap_units) is not int or cap_units <= 0
                or not re.fullmatch(r'[0-9a-f]{64}', code_sha256)):
            raise GlobalStop('Explicit state repository/policy/code identity required')
        self.git, self.repository, self.branch = git, repository, branch
        self.period, self.cap, self.code, self.clock = period, cap_units, code_sha256, clock
        self.head = self.manifest = None
        self.files = {}
        self.poisoned = False

    def private(self):
        info = self.git.repository()
        if info.get('full_name') != self.repository or info.get('private') is not True:
            raise GlobalStop('State repository must be the exact private repository')

    def healthy(self):
        if self.poisoned:
            raise GlobalStop('Storage outcome requires fresh inspection')

    def load(self):
        self.healthy(); self.private()
        h = self.git.head(self.branch)
        if h is None:
            raise GlobalStop('State branch missing; no automatic bootstrap')
        manifest, files = self._read(valid_sha(h))
        self.head, self.manifest, self.files = h, manifest, files
        return copy.deepcopy(manifest), dict(files)

    def _read(self, head, known_files=None):
        commit = self.git.commit(head)
        if commit.get('sha') != head:
            raise GlobalStop('State commit mismatch')
        entries = self.git.tree(valid_sha(commit['tree']['sha']))
        if len(entries) > MAX_FILES + 1 or len({x['path'] for x in entries}) != len(entries):
            raise GlobalStop('State tree inventory')
        by_name = {}
        for x in entries:
            if x.get('type') != 'blob' or x.get('mode') != '100644':
                raise GlobalStop('State tree contains unsupported entry')
            by_name[x['path']] = valid_sha(x['sha'])
        if 'manifest.json' not in by_name:
            raise GlobalStop('State manifest missing')
        raw = self.git.blob(by_name['manifest.json'])
        if len(raw) > 150000 or blob_sha(raw) != by_name['manifest.json']:
            raise GlobalStop('State manifest content mismatch')
        try:
            m = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise GlobalStop('State manifest is unreadable') from exc
        fields = {'version','repository','branch','period','cap_units','code_sha256','sequence','previous_commit','owner','created_utc','files'}
        if (not isinstance(m,dict) or set(m) != fields or m['version'] != VERSION or m['repository'] != self.repository
                or m['branch'] != self.branch or m['period'] != self.period or m['cap_units'] != self.cap
                or m['code_sha256'] != self.code or type(m['sequence']) is not int or m['sequence'] < 0):
            raise GlobalStop('Stored state policy/schema/code changed')
        instant(m['created_utc'])
        if m['owner'] is not None:
            o=m['owner']
            if not isinstance(o,dict) or set(o)!={'run_id','run_attempt','nonce'} or owner_id(o['run_id'],o['run_attempt'],o['nonce'])!=o:
                raise GlobalStop('State owner identity')
        parents = [p['sha'] for p in commit['parents']]
        if (m['sequence'] == 0 and (parents or m['previous_commit'] is not None)
                or m['sequence'] > 0 and parents != [m['previous_commit']]):
            raise GlobalStop('State commit parent mismatch')
        if not isinstance(m['files'],dict) or set(by_name) != {'manifest.json', *m['files']}:
            raise GlobalStop('State tree/manifest differ')
        if any(not isinstance(d,dict) or set(d)!={'size','sha256','git_blob'} or type(d['size']) is not int or not 0<d['size']<=MAX_FILE for d in m['files'].values()):
            raise GlobalStop('State inventory metadata')
        if sum(d['size'] for d in m['files'].values())>MAX_TOTAL:
            raise GlobalStop('State snapshot size limit')
        files = {}
        for name, description in m['files'].items():
            if (not NAME.fullmatch(name) or set(description) != {'size','sha256','git_blob'}
                    or type(description['size']) is not int or not 0 < description['size'] <= MAX_FILE
                    or description['git_blob'] != by_name[name]):
                raise GlobalStop('State inventory content contract')
            if sum(d['size'] for d in m['files'].values()) > MAX_TOTAL:
                raise GlobalStop('State snapshot size limit')
            # Reuse only byte-verified content-addressed local data. A new
            # process still fetches and verifies every manifest member.
            cached=(known_files if known_files is not None else self.files).get(name)
            data=cached if cached is not None and blob_sha(cached)==by_name[name] else self.git.blob(by_name[name])
            if len(data) != description['size'] or sha256(data) != description['sha256'] or blob_sha(data) != by_name[name]:
                raise GlobalStop('State file checksum mismatch')
            files[name] = data
        validate_files(files)
        return m, files

    def assert_local_owner(self, owner):
        """Cheap local invariant only; NEVER authorizes an external effect."""
        self.healthy()
        if self.head is None or self.manifest['owner'] != owner:
            raise GlobalStop('State ownership or branch changed')

    def assert_owner(self, owner):
        self.assert_local_owner(owner)
        if self.git.head(self.branch) != self.head:
            raise GlobalStop('State ownership or branch changed')

    def bootstrap(self, files):
        """Explicit fresh branch initialization, separate from load/recovery."""
        self.healthy(); self.private()
        if self.head is not None or self.git.head(self.branch) is not None:
            raise GlobalStop('Bootstrap cannot replace existing state')
        return self._write(files, None, bootstrap=True)

    def claim(self, owner):
        self.healthy()
        if self.head is None or self.manifest['owner'] is not None:
            raise GlobalStop('Active or interrupted owner cannot be stolen')
        if owner_id(owner['run_id'],owner['run_attempt'],owner['nonce']) != owner:
            raise GlobalStop('State owner identity')
        return self._write(self.files, owner)

    def checkpoint(self, files, owner):
        # _write checks the remote head and publishes by non-force update.
        self.assert_local_owner(owner)
        return self._write(files, owner)

    def claim_recovery(self, owner, terminal_run):
        """Explicit readback-only recovery after the owning workflow completed.

        terminal_run must come from the fixed-repository GitHub run endpoint.
        Age or a cancelled local process is never evidence of completion.
        """
        self.healthy()
        old=self.manifest['owner'] if self.manifest else None
        if (old is None or owner==old or terminal_run.get('id')!=int(old['run_id'])
                or terminal_run.get('run_attempt')!=old['run_attempt']
                or terminal_run.get('status')!='completed'
                or terminal_run.get('repository',{}).get('full_name')!=self.repository):
            raise GlobalStop('Completed owning workflow must be verified before recovery')
        if owner_id(owner['run_id'],owner['run_attempt'],owner['nonce'])!=owner:
            raise GlobalStop('Recovery owner identity')
        return self._write(self.files, owner)

    def release(self, owner):
        self.assert_owner(owner)
        return self._write(self.files, None)

    def _write(self, files, owner, bootstrap=False):
        validate_files(files); self.private()
        previous = self.head
        if self.git.head(self.branch) != previous:
            raise GlobalStop('Competing state update; do not rebase stale state')
        m = dict(version=VERSION,repository=self.repository,branch=self.branch,period=self.period,
                 cap_units=self.cap,code_sha256=self.code,sequence=0 if bootstrap else self.manifest['sequence']+1,
                 previous_commit=previous,owner=copy.deepcopy(owner),created_utc=self.clock().isoformat(),
                 files={n:dict(size=len(b),sha256=sha256(b),git_blob=blob_sha(b)) for n,b in sorted(files.items())})
        instant(m['created_utc'])
        objects = dict(files, **{'manifest.json':canonical(m)})
        entries=[]
        try:
            for name,data in sorted(objects.items()):
                blob = blob_sha(data)
                if self.files.get(name) != data:
                    if self.git.create_blob(data) != blob:
                        raise GlobalStop('Created blob identity mismatch')
                entries.append(dict(path=name,mode='100644',type='blob',sha=blob))
            tree = valid_sha(self.git.create_tree(entries))
            target = valid_sha(self.git.create_commit(tree, [] if previous is None else [previous]))
            # There is never a retry of a write with unknown outcome.
            try:
                if bootstrap:self.git.create_ref(self.branch,target)
                else:self.git.update_ref(self.branch,target,force=False)
            except Exception:
                pass  # Read exactly this target; conflicting/absent state stops.
            # Git ref reads may briefly return the previous head after PATCH.
            # Retry GET only, never the write. A competing head stops at once.
            for delay in (0, .25, .75, 1.5):
                if delay:time.sleep(delay)
                observed = self.git.head(self.branch)
                if observed == target:break
                if observed != previous:
                    raise GlobalStop('Competing checkpoint readback')
            else:
                raise GlobalStop('State checkpoint not confirmed')
            # Newly uploaded bytes are usable only after independently reading
            # the exact commit/tree/manifest and checking all content identities.
            # A fresh process has no such cache and downloads every member.
            got, content = self._read(target, known_files=files)
            if got != m or content != files:
                raise GlobalStop('State checkpoint readback differs')
            self.head,self.manifest,self.files=target,m,dict(files)
            return target
        except BaseException as exc:
            self.poisoned=True
            if isinstance(exc,GlobalStop):raise
            raise GlobalStop('State storage failed; no external action permitted') from exc
