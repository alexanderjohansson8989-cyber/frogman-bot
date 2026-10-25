"""Lossless immutable JSON objects. Shared subtrees are stored once, not per event.

Caller owns the SQLite transaction. Old external event archives remain readable;
this module never deletes them or migrates a live journal automatically.
"""
import json
import re
import sqlite3
import zlib
from git_state_store_v1 import canonical, sha256
from operations_ledger_v1 import GlobalStop

VERSION = 'frogman-checkpoint-objects-v1'
MAX_BYTES = 32 * 1024 * 1024
MAX_NODES = 100000
MAX_DEPTH = 100
HASH = re.compile(r'[0-9a-f]{64}')


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS checkpoint_codec (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO checkpoint_codec VALUES(1,?)', (VERSION,))
    if db.execute('SELECT version FROM checkpoint_codec WHERE singleton=1').fetchone() != (VERSION,):
        raise GlobalStop('Unknown checkpoint object codec')
    db.execute('CREATE TABLE IF NOT EXISTS checkpoint_objects (sha256 TEXT PRIMARY KEY, compressed BLOB NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS checkpoint_roots (sequence INTEGER PRIMARY KEY, root_sha256 TEXT NOT NULL)')


def pack(db, value):
    # Validate the complete event before writing any subtree, including NaN.
    if len(canonical(value)) > MAX_BYTES:
        raise GlobalStop('Checkpoint event size limit')
    seen = set()
    count = 0
    def visit(v, depth):
        nonlocal count
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise GlobalStop('Checkpoint object complexity limit')
        if type(v) is dict:
            if any(type(k) is not str for k in v):
                raise GlobalStop('Checkpoint object keys must be strings')
            node = ['dict', [[k, visit(v[k], depth+1)] for k in sorted(v)]]
        elif type(v) is list:
            node = ['list', [visit(x, depth+1) for x in v]]
        elif v is None or type(v) in (str, int, float, bool):
            node = ['scalar', v]
        else:
            raise GlobalStop('Unsupported checkpoint object')
        raw = canonical(node)
        h = sha256(raw)
        if h not in seen:
            existing = db.execute('SELECT compressed FROM checkpoint_objects WHERE sha256=?', (h,)).fetchone()
            if existing is None:
                db.execute('INSERT INTO checkpoint_objects VALUES(?,?)', (h, zlib.compress(raw)))
            elif inflate(existing[0]) != raw:
                raise GlobalStop('Checkpoint object collision or corruption')
            seen.add(h)
        return h
    return visit(value, 0)


def inflate(data):
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(data, MAX_BYTES+1)
        if len(raw) > MAX_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise GlobalStop('Invalid compressed checkpoint object')
        return raw
    except zlib.error as exc:
        raise GlobalStop('Corrupt compressed checkpoint object') from exc


def unpack(db, root):
    count = 0
    expanded = 0
    def visit(h, depth):
        nonlocal count, expanded
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES or not isinstance(h, str) or not HASH.fullmatch(h):
            raise GlobalStop('Invalid checkpoint object reference')
        row = db.execute('SELECT compressed FROM checkpoint_objects WHERE sha256=?', (h,)).fetchone()
        if row is None:
            raise GlobalStop('Missing checkpoint object')
        raw = inflate(row[0])
        expanded += len(raw)
        if expanded > MAX_BYTES or sha256(raw) != h:
            raise GlobalStop('Checkpoint object integrity/size failure')
        try:
            node = json.loads(raw)
            if canonical(node) != raw or type(node) is not list or len(node) != 2:
                raise ValueError()
            kind, body = node
            if kind == 'scalar' and (body is None or type(body) in (str, int, float, bool)):
                return body
            if kind == 'list' and type(body) is list:
                return [visit(x, depth+1) for x in body]
            if kind == 'dict' and type(body) is list:
                if any(type(x) is not list or len(x) != 2 or type(x[0]) is not str for x in body):
                    raise ValueError()
                keys = [x[0] for x in body]
                if keys != sorted(set(keys)):
                    raise ValueError()
                return {k: visit(v, depth+1) for k, v in body}
            raise ValueError()
        except (ValueError, TypeError) as exc:
            raise GlobalStop('Invalid checkpoint object encoding') from exc
    return visit(root, 0)


def read_event(db, sequence, archives):
    row = db.execute('SELECT created_utc,action,archive_sha256 FROM checkpoint_events WHERE sequence=?', (sequence,)).fetchone()
    if row is None:
        raise GlobalStop('Missing checkpoint event')
    roots = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoint_roots'").fetchone()
    root = db.execute('SELECT root_sha256 FROM checkpoint_roots WHERE sequence=?', (sequence,)).fetchone() if roots else None
    if root:
        if db.execute('SELECT version FROM checkpoint_codec WHERE singleton=1').fetchone() != (VERSION,):
            raise GlobalStop('Unknown checkpoint object codec')
        event = unpack(db, root[0])
        raw = canonical(event)
    else:
        raw = archives.get('a-'+row[2]+'.json')
        if not isinstance(raw, bytes) or sha256(raw) != row[2]:
            raise GlobalStop('Missing or corrupt legacy event archive')
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise GlobalStop('Invalid legacy event archive') from exc
    if (sha256(raw) != row[2] or type(event) is not dict or
            set(event) != {'created_utc','action','arguments'} or
            event['created_utc'] != row[0] or event['action'] != row[1]):
        raise GlobalStop('Checkpoint event identity mismatch')
    return event


def events_from_snapshot(files):
    db=sqlite3.connect(':memory:')
    try:
        db.deserialize(files['journal.sqlite3'])
        return [read_event(db,n,files) for (n,) in db.execute('SELECT sequence FROM checkpoint_events ORDER BY sequence')]
    finally:
        db.close()
