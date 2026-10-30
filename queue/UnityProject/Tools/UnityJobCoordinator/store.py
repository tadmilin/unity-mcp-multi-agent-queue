"""Durable local jobs. SQLite transactions never span an Editor operation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

SCHEMA = 1
TERMINAL = ('succeeded', 'failed', 'cancelled', 'abandoned')


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def redact(value):
    if isinstance(value, dict):
        return {k: '<redacted>' if k in ('token', 'secretHash', 'base64') else redact(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def target_path(root, relative):
    if not isinstance(relative, str) or not relative.startswith('Assets/'):
        raise ValueError('Destination must start with Assets/')
    parts = relative.split('/')
    if any(p in ('', '.', '..') or any(c in p for c in '\\:*?"<>|')
           or p.endswith((' ', '.')) for p in parts):
        raise ValueError('Invalid destination path')
    if hasattr(os.path, 'isreserved') and any(os.path.isreserved(p) for p in parts):
        raise ValueError('Reserved destination name')
    root = Path(root).resolve()
    candidate = root.joinpath(*parts)
    for parent in [candidate, *candidate.parents]:
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and
                getattr(parent.lstat(), 'st_file_attributes', 0) & 1024):
            raise ValueError('Reparse points are not supported')
    if not candidate.resolve().is_relative_to(root):
        raise ValueError('Destination escapes workspace')
    return candidate


def snapshot_manifest(root, path):
    """Copy source bytes into a canonical job before accepting it."""
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(raw, dict) or set(raw) - {'kind', 'files', 'depends_on'}:
        raise ValueError('Unknown manifest fields')
    kind = raw.get('kind', 'files')
    if kind not in ('files', 'check'):
        raise ValueError('kind must be files or check')
    files = raw.get('files', [])
    if not isinstance(files, list) or (kind == 'files' and not 1 <= len(files) <= 32):
        raise ValueError('A files job requires 1..32 files')
    if kind == 'check' and files:
        raise ValueError('check jobs cannot write files')
    dependencies = raw.get('depends_on', [])
    if (not isinstance(dependencies, list) or len(dependencies) > 32 or
            any(not isinstance(d, str) for d in dependencies)):
        raise ValueError('depends_on must contain up to 32 job IDs')
    result, seen, size = [], set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) - {'path', 'source', 'delete', 'expectedSha256'}:
            raise ValueError('Unknown file fields')
        relative = item.get('path')
        target_path(root, relative)
        if relative.casefold() in seen:
            raise ValueError('Duplicate destination')
        seen.add(relative.casefold())
        expected = item.get('expectedSha256', '')
        if not isinstance(expected, str) or not re.fullmatch(r'missing|[0-9a-f]{64}', expected):
            raise ValueError('Explicit expectedSha256 required; use missing for new files')
        entry = {'path': relative, 'expectedSha256': expected}
        if item.get('delete') is True and 'source' not in item:
            entry['delete'] = True
        elif 'source' in item and 'delete' not in item:
            source = (path.parent / item['source']).resolve()
            if source.stat().st_size + size > 10 * 1024 * 1024:
                raise ValueError('Payload exceeds 10 MiB')
            content = source.read_bytes()
            size += len(content)
            if size > 10 * 1024 * 1024:
                raise ValueError('Payload exceeds 10 MiB')
            entry['base64'] = base64.b64encode(content).decode('ascii')
        else:
            raise ValueError('Specify source or delete:true')
        result.append(entry)
    return {'kind': kind, 'files': result, 'depends_on': sorted(set(dependencies))}


class Store:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.home / 'queue.sqlite3', timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, SCHEMA):
            self.db.close()
            raise ValueError('Unsupported database schema; no changes made')
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS jobs ('
                'id TEXT PRIMARY KEY, submit_key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL, '
                'manifest TEXT NOT NULL, state TEXT NOT NULL, phase TEXT NOT NULL, '
                'created REAL NOT NULL, started REAL, claimed REAL, finished REAL, '
                'token TEXT, ticket INTEGER, result TEXT)')
            self.db.execute('CREATE TABLE IF NOT EXISTS events ('
                'seq INTEGER PRIMARY KEY, job TEXT NOT NULL, time REAL NOT NULL, '
                'kind TEXT NOT NULL, detail TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS controls (key TEXT PRIMARY KEY, value TEXT)')
            self.db.execute(f'PRAGMA user_version={SCHEMA}')

    def close(self):
        self.db.close()

    def event(self, job, kind, detail):
        with self.db:
            self.db.execute('INSERT INTO events(job,time,kind,detail) VALUES(?,?,?,?)',
                            (job, time.time(), kind, encode(redact(detail))))

    def control(self, key, value=None):
        if value is None:
            row = self.db.execute('SELECT value FROM controls WHERE key=?', (key,)).fetchone()
            return row[0] if row else None
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO controls VALUES(?,?)', (key, str(value)))

    def submit(self, key, manifest):
        if not key or len(key) > 120:
            raise ValueError('Submission key must contain 1..120 characters')
        content = encode(manifest)
        checksum = digest(content.encode())
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old = self.db.execute('SELECT id,digest FROM jobs WHERE submit_key=?', (key,)).fetchone()
            if old:
                if old['digest'] != checksum:
                    raise ValueError('Submission key already used for different content')
                return old['id']
            for dependency in manifest['depends_on']:
                if not self.db.execute('SELECT 1 FROM jobs WHERE id=?', (dependency,)).fetchone():
                    raise ValueError('Dependency does not exist: ' + dependency)
            job = uuid.uuid4().hex
            self.db.execute('INSERT INTO jobs(id,submit_key,digest,manifest,state,phase,created) '
                            'VALUES(?,?,?,?,?,?,?)',
                            (job, key, checksum, content, 'queued', 'queued', time.time()))
            return job

    def get(self, job):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if row is None:
            raise ValueError('Unknown job: ' + job)
        return dict(row)

    def update(self, job, **fields):
        allowed = {'state', 'phase', 'started', 'claimed', 'finished', 'token', 'ticket', 'result'}
        if not fields or set(fields) - allowed:
            raise ValueError('Invalid job update')
        with self.db:
            self.db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                            [*fields.values(), job])

    def interrupted(self):
        with self.db:
            self.db.execute("UPDATE jobs SET state='quarantined', result=? WHERE state='running'",
                            (encode({'reason': 'Worker interrupted; inspect receipts before recovery'}),))

    def next_job(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute("SELECT 1 FROM jobs WHERE state IN ('running','quarantined')").fetchone():
                return None
            for row in self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created,id").fetchall():
                manifest = json.loads(row['manifest'])
                states = [self.get(d)['state'] for d in manifest['depends_on']]
                if any(s in ('failed', 'cancelled', 'abandoned') for s in states):
                    self.db.execute("UPDATE jobs SET state='failed',phase='dependency',finished=?,result=? WHERE id=?",
                                    (time.time(), encode({'reason': 'Dependency did not succeed'}), row['id']))
                    continue
                if any(s != 'succeeded' for s in states):
                    continue
                self.db.execute("UPDATE jobs SET state='running',phase='preflight',started=? WHERE id=?",
                                (time.time(), row['id']))
                return row['id']
        return None

    def public(self, job, events=True):
        row = self.get(job)
        row.pop('token')
        row['kind'] = json.loads(row.pop('manifest'))['kind']
        row['result'] = json.loads(row['result']) if row['result'] else None
        row['queue_seconds'] = row['started'] - row['created'] if row['started'] else None
        row['gate_wait_seconds'] = row['claimed'] - row['started'] if row['claimed'] else None
        row['total_seconds'] = row['finished'] - row['created'] if row['finished'] else None
        recorded = [dict(e) | {'detail': json.loads(e['detail'])}
            for e in self.db.execute('SELECT * FROM events WHERE job=? ORDER BY seq', (job,))]
        imports = [e['time'] for e in recorded if e['kind'] == 'intent' and e['detail'].get('action') == 'import']
        complete = [e['time'] for e in recorded if e['kind'] == 'status' and
            (e['detail'].get('import') or {}).get('state') == 'complete' and
            (e['detail'].get('import') or {}).get('operation') == job + '-write']
        row['import_seconds'] = min(complete) - imports[0] if imports and complete else None
        if events:
            row['events'] = recorded
        return row


class WorkerLock:
    """OS lock is released by process exit, including crashes. No PID killing."""
    def __init__(self, home):
        self.path = Path(home) / 'worker.lock'
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError('Worker already running') from None
        return self

    def __exit__(self, *args):
        if self.stream:
            if os.name == 'nt':
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()


def worker_running(home):
    try:
        with WorkerLock(home):
            return False
    except RuntimeError:
        return True
