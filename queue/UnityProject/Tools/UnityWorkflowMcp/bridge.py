"""A blocking MCP-facing observer over the existing coordinator, schema unchanged.

Only submission mutates the queue. Waiting reads SQLite directly, never invokes a
shell/CLI, and never requests a second Editor token or replays a Unity operation.
"""
import asyncio
from collections import Counter
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[2]
COORDINATOR = PROJECT / 'Tools/UnityJobCoordinator'
spec = importlib.util.spec_from_file_location('workflow_coordinator_store', COORDINATOR / 'store.py')
coordinator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coordinator)
FINAL = frozenset((*coordinator.TERMINAL, 'quarantined'))
KNOWN = FINAL | {'queued', 'running'}


class Bridge:
    durable_quarantine_wait = False  # Legacy executor retains its original contract.
    def __init__(self, root=PROJECT, home=None, worker_check=None, interval=0.25):
        self.root = Path(root).resolve()
        self.home = Path(home).resolve() if home else self.root / 'JobQueue'
        self.worker_check = worker_check or (lambda: coordinator.worker_running(self.home))
        self.interval = interval

    def quarantine_blockers(self):
        """Both executors share one Editor. Inspect both ledgers before waiting."""
        blockers = []
        homes = {self.home, self.root / 'JobQueue', self.root / 'JobQueue/phases'}
        for home in sorted(homes):
            path = home / 'queue.sqlite3'
            if not path.is_file():
                continue
            with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
                for job, ticket, result in db.execute("SELECT id,ticket,result FROM jobs WHERE state='quarantined'"):
                    blockers.append({'job_id': job, 'ticket': ticket,
                                     'mode': 'phase' if home.name == 'phases' else 'job',
                                     'reason': json.loads(result or '{}').get('reason', 'Quarantined')})
        return blockers

    def connection(self):
        # Observation must not create/migrate a database or alter queue controls.
        uri = (self.home / 'queue.sqlite3').as_uri() + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        if connection.execute('PRAGMA user_version').fetchone()[0] != coordinator.SCHEMA:
            connection.close()
            raise ValueError('Unsupported coordinator schema')
        return connection

    def inside(self, name):
        if not isinstance(name, str) or not name or Path(name).is_absolute():
            raise ValueError('Use a path relative to the fixed workspace')
        path = self.root / name
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('Path escapes workspace')
        for item in [path, *path.parents]:
            if item == self.root:
                break
            if item.is_symlink() or (item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 1024):
                raise ValueError('Reparse points are not supported')
        return path.resolve()

    def prepare(self, manifest_path):
        path = self.inside(manifest_path)
        relative = path.relative_to(self.root)
        if relative.parts[0].casefold() in ('assets', 'packages', 'projectsettings'):
            raise ValueError('Stage manifests outside Unity import roots')
        if path.stat().st_size > 256 * 1024:
            raise ValueError('Manifest exceeds 256 KiB')
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(raw, dict):
            raise ValueError('Manifest must be an object')
        for entry in raw.get('files', []):
            if isinstance(entry, dict) and 'source' in entry:
                source = entry['source']
                if not isinstance(source, str):
                    raise ValueError('Source must be text')
                # Manifest-relative sources, pinned to a validated absolute path
                # in our immutable temporary descriptor before canonicalization.
                candidate = path.parent / source
                try:
                    source_name = candidate.relative_to(self.root).as_posix()
                except ValueError:
                    raise ValueError('Source escapes workspace') from None
                entry['source'] = str(self.inside(source_name))
        staging = self.home / 'mcp-staging'
        staging.mkdir(exist_ok=True)
        descriptor = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json',
                                             dir=staging, delete=False) as stream:
                json.dump(raw, stream)
                descriptor = Path(stream.name)
            return coordinator.snapshot_manifest(self.root, descriptor)
        finally:
            if descriptor is not None:
                descriptor.unlink(missing_ok=True)

    def validate_new_submission(self, manifest):
        pass

    def submit(self, submission_key, manifest_path):
        if not isinstance(submission_key, str) or not 1 <= len(submission_key) <= 120:
            raise ValueError('Submission key must contain 1..120 characters')
        if not (self.home / 'installed.json').is_file():
            raise ValueError('Coordinator is not installed')
        manifest = self.prepare(manifest_path)
        # Reuse the same canonical payload and atomic key/digest transaction as
        # queue.cmd submit. This is a new ingress, not a second queue or worker.
        with contextlib.closing(self.connection()) as connection:
            old = connection.execute('SELECT id,digest FROM jobs WHERE submit_key=?', (submission_key,)).fetchone()
            if old:
                if old['digest'] != coordinator.digest(coordinator.encode(manifest).encode()):
                    raise ValueError('Submission key already exists with different content')
                return old['id']
            self.validate_new_submission(manifest)
            blockers = self.quarantine_blockers()
            if blockers and not self.durable_quarantine_wait:
                raise ValueError('Queue has quarantine across executors: EDITOR_QUEUE_QUARANTINE_BLOCKED: ' + json.dumps(blockers)
                                 + '; no job submitted; central cleanup recovery observes eligible phases')
            if not self.worker_check():
                raise ValueError('Worker is not running; no job submitted')
            stop = connection.execute("SELECT value FROM controls WHERE key='stop'").fetchone()
            if stop and stop[0] == '1':
                raise ValueError('Worker is stopping; no job submitted')
            if not self.durable_quarantine_wait and connection.execute("SELECT 1 FROM jobs WHERE state='quarantined'").fetchone():
                raise ValueError('Queue has quarantine; no job submitted')
            for dependency in manifest['depends_on']:
                if connection.execute('SELECT 1 FROM jobs WHERE id=?', (dependency,)).fetchone() is None:
                    raise ValueError('Unknown dependency; no job submitted')
        with contextlib.closing(coordinator.Store(self.home)) as store:
            return store.submit(submission_key, manifest)

    def snapshot(self, job_id=None, submission_key=None, evidence=False):
        if (job_id is None) == (submission_key is None):
            raise ValueError('Supply exactly one of job_id or submission_key')
        if job_id is not None and not re.fullmatch('[0-9a-f]{32}', job_id):
            raise ValueError('Invalid job ID')
        with contextlib.closing(self.connection()) as connection:
            row = connection.execute(
                'SELECT id,submit_key,state,phase,created,started,finished,ticket,result,digest '
                'FROM jobs WHERE ' + ('id=?' if job_id is not None else 'submit_key=?'),
                (job_id if job_id is not None else submission_key,)).fetchone()
            if row is None:
                raise ValueError('Unknown job')
            result = dict(row)
            result['result'] = json.loads(result['result']) if result['result'] else None
            if evidence:
                intents = [json.loads(event[0]).get('action', 'unknown') for event in connection.execute(
                    "SELECT detail FROM events WHERE job=? AND kind='intent'", (result['id'],))]
                responses = [json.loads(event[0]) for event in connection.execute(
                    "SELECT detail FROM events WHERE job=? AND kind='response'", (result['id'],))]
                result['evidence'] = {
                    'event_counts': dict(connection.execute('SELECT kind,COUNT(*) FROM events WHERE job=? GROUP BY kind', (result['id'],))),
                    'intent_counts': dict(Counter(intents)),
                    'last_sequence': connection.execute('SELECT MAX(seq) FROM events WHERE job=?', (result['id'],)).fetchone()[0],
                    'release_confirmed': any(event.get('action') == 'release' and
                        event.get('response', {}).get('success') is True for event in responses)}
        return coordinator.redact(result)

    @staticmethod
    def outcome(snapshot, timed_out=False):
        state = snapshot['state']
        console = (snapshot.get('result') or {}).get('console') or {}
        entries = console.get('data')
        # The existing worker checks the console RPC success, not the entries.
        # Preserve its state, but never call nonempty/unparseable errors verified.
        clear_console = console.get('success') is True and isinstance(entries, list) and not entries
        released = snapshot.get('evidence', {}).get('release_confirmed') is True
        accepted = state == 'succeeded' and clear_console and released
        reason = ('completed' if accepted else 'console_not_verified' if state == 'succeeded' and not clear_console
                  else 'release_not_verified' if state == 'succeeded' and not released
                  else 'observation_timeout' if timed_out else state if state in KNOWN else 'unknown_state')
        snapshot = dict(snapshot)
        raw_result = snapshot.get('result') or {}
        snapshot['result'] = {'scope': raw_result.get('scope'),
            'cleanup_recovered': raw_result.get('cleanup_recovered', False),
            'original_failure': raw_result.get('original_result'),
            'reason': str(raw_result.get('reason', ''))[:2048],
            'console': {'success': console.get('success'),
                        'error_count': len(entries) if isinstance(entries, list) else None,
                        'sample_errors': [str(entry)[:600] for entry in entries[:3]] if isinstance(entries, list) else []},
            'editor_before_release': raw_result.get('editor_before_release')}
        return {'schema': 'unity-workflow-mcp-v1', 'job_id': snapshot['id'],
                'task_verified_success': None,
                'completion_check_count': 0, 'completion_checks_passed': False,
                'submission_key': snapshot['submit_key'], 'state': state, 'phase': snapshot['phase'],
                'completed': state in coordinator.TERMINAL, 'verified_success': accepted, 'reason': reason,
                'attention_required': state == 'quarantined' or state not in KNOWN,
                'cleanup_recovered': raw_result.get('cleanup_recovered', False),
                'observation_timed_out': timed_out, 'job_cancelled_by_observer': False,
                'scope': 'coordinator gate/import/hash, release receipt and empty error-console; no gameplay or loaded-type oracle',
                'recovery': 'Use the same submission key or unity_job_result; never invent a new key after lost response',
                'snapshot': snapshot}

    async def wait(self, job_id, wait_seconds):
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 1800:
            raise ValueError('wait_seconds must be between 0 and 1800')
        deadline = time.monotonic() + wait_seconds
        while True:
            snapshot = await asyncio.to_thread(self.snapshot, job_id)
            if snapshot['state'] in FINAL or snapshot['state'] not in KNOWN:
                return self.outcome(await asyncio.to_thread(self.snapshot, job_id, None, True))
            blockers = await asyncio.to_thread(self.quarantine_blockers)
            if blockers:
                # A job may have become terminal during the cross-ledger read.
                # Never report its stale running snapshot as blocked by itself.
                snapshot = await asyncio.to_thread(self.snapshot, job_id, None, True)
                if snapshot['state'] in FINAL or snapshot['state'] not in KNOWN:
                    return self.outcome(snapshot)
                if not self.durable_quarantine_wait or time.monotonic() >= deadline:
                    value = self.outcome(snapshot, timed_out=time.monotonic() >= deadline)
                    value.update(attention_required=True, reason='EDITOR_QUEUE_QUARANTINE_BLOCKED',
                                 blocked_by=blockers, job_cancelled_by_observer=False,
                                 recovery='Durable job retained; central recovery handles eligible cleanup. Query this same key.')
                    return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.outcome(snapshot, timed_out=True)
            await asyncio.sleep(min(self.interval, remaining))

    async def run(self, submission_key, manifest_path, wait_seconds=600):
        # Validate before acceptance. Cancellation after acceptance leaves the
        # durable job intact; it does not cancel, duplicate, or replay its work.
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or not 0 <= wait_seconds <= 1800:
            raise ValueError('wait_seconds must be between 0 and 1800')
        job_id = await asyncio.to_thread(self.submit, submission_key, manifest_path)
        return await self.wait(job_id, wait_seconds)

    def capabilities(self):
        return {'schema': 'unity-workflow-mcp-v1', 'workspace': str(self.root),
                'cleanup_recovery': 'owned-delayed-stop-idle-v1',
                'cross_executor_quarantine_admission': True,
                'coordinator_schema': coordinator.SCHEMA, 'worker_running': self.worker_check(),
                'store_sha256': hashlib.sha256((COORDINATOR / 'store.py').read_bytes()).hexdigest(),
                'single_call_wait': True, 'wait_limit_seconds': 1800,
                'wait_implementation': 'server-side read-only SQLite observation; no shell or CLI polling',
                'starts_or_stops_worker': False, 'closed_turn_resume': 'not_qualified',
                'supported_kinds': ['files', 'check']}
