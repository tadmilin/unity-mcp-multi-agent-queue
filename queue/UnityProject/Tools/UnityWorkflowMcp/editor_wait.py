"""Owned Play transitions: directory events first, live MCP confirmation second.

Signals never grant authority. The command and its acknowledgement are unchanged;
an uncertain confirmation is not retried or converted into permission to cleanup.
"""
from hashlib import sha256
import math
from pathlib import Path
import time

from import_wait import DirectoryChanges, ImportWaitError, validate_connection
from initial_wait import READINESS_SCHEMA, live_unity_process

VERSION = 'owned-play-notification-v1'
LOADED_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()


class EditorWaitError(RuntimeError):
    pass


class EditorWaitDeadline(EditorWaitError):
    """Known transition budget expired; distinct from an uncertain command."""


class EditorWait:
    def __init__(self, worker, job, label, changes=DirectoryChanges,
                 process_check=live_unity_process):
        self.worker, self.job = worker, job
        self.operation = job + '-' + label
        self.gate = worker.root / 'Library/EditorQueueGate/state.json'
        self.ready = worker.root / 'Library/UnityWorkflowSignals/editor-readiness.json'
        self.native = self.ready.parent / 'native-connected.json'
        self.ticket = worker.store.get(job)['ticket']
        self.owner = 'coordinator-' + job
        self.secret = sha256(worker.client.token.encode()).hexdigest()
        self.pid = worker.claim_initial['editor']['pid']
        self.changes = changes([self.gate.parent, self.ready.parent])
        self.process_check = process_check
        self.reads, self.wakes, self.confirmations = 0, 0, 0
        self.receipt_hash = None
        self.deadline = None
        self.last_confirmed = None
        try:
            self.connection = validate_connection(self.changes.read(self.native), worker.root)
            self.before = self.read_ready()
            worker.store.event(job, 'editor_wait_armed', {
                'version': VERSION, 'operation': self.operation, 'ticket': self.ticket,
                'pid': self.pid, 'epoch': self.before['epoch'] if self.before else None})
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.changes.close()

    def read_ready(self):
        stamp = self.ready.stat().st_mtime_ns
        value = self.changes.read(self.ready)
        self.reads += 1
        if self.ready.stat().st_mtime_ns != stamp:
            return None
        if (not isinstance(value, dict) or value.get('schema') != READINESS_SCHEMA or
                Path(value.get('workspace', '')).resolve() != self.worker.root.resolve() or
                type(value.get('pid')) is not int or value['pid'] != self.pid or
                not isinstance(value.get('epoch'), str) or not value['epoch'] or
                type(value.get('sequence')) is not int or value['sequence'] < 1 or
                any(type(value.get(flag)) is not bool for flag in
                    ('idle', 'unavailable', 'dirty', 'playing', 'changing', 'compiling', 'updating')) or
                not self.process_check(value['pid'], stamp)):
            raise EditorWaitError('Play readiness publisher identity changed; no replay')
        return value

    def owned_snapshot(self):
        value = self.changes.read(self.gate)
        holder = value.get('holder') or {}
        receipt = holder.get('operation') or {}
        expiry = holder.get('deadline')
        if (value.get('version') != 1 or holder.get('ticket') != self.ticket or
                holder.get('owner') != self.owner or holder.get('secretHash') != self.secret or
                holder.get('blocked') or type(expiry) not in (int, float) or
                not math.isfinite(expiry) or expiry <= time.time() or
                value.get('import') is not None or receipt.get('id') != self.operation or
                receipt.get('state') != 'acknowledged' or not receipt.get('sha256') or
                (receipt.get('result') or {}).get('success') is not True):
            raise EditorWaitError('Play transition ownership/receipt changed; no replay')
        if self.receipt_hash is not None and receipt['sha256'] != self.receipt_hash:
            raise EditorWaitError('Play transition receipt hash changed; no replay')
        self.receipt_hash = receipt['sha256']
        # Observing a renewed lease must never extend this transition's budget.
        self.deadline = min(self.deadline, time.monotonic() + expiry - time.time())
        return holder

    def confirm(self, predicate):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EditorWaitDeadline('Editor transition deadline exceeded')
        transport = self.worker.client.client
        old_timeout = transport.timeout
        try:
            transport.timeout = min(old_timeout, remaining)
            self.confirmations += 1
            response = self.worker.rpc(self.job, 'status')
        finally:
            transport.timeout = old_timeout
        state = self.worker.require(response)
        self.worker.store.event(self.job, 'status', state)
        holder, receipt = state.get('holder') or {}, state.get('receipt') or {}
        if (holder.get('ticket') != self.ticket or
                holder.get('owner') != self.owner or holder.get('blocked') or
                holder.get('deadline', 0) <= time.time() or state.get('import') is not None or
                receipt.get('id') != self.operation or receipt.get('state') != 'acknowledged' or
                receipt.get('sha256') != self.receipt_hash or
                (receipt.get('result') or {}).get('success') is not True or
                state['editor'].get('pid') != self.pid):
            raise EditorWaitError('Live Play transition confirmation mismatched or late; no replay')
        if time.monotonic() >= self.deadline:
            raise EditorWaitDeadline('Editor transition deadline exceeded; late live response')
        self.worker.store.event(self.job, 'editor_wait_confirmation', {
            'operation': self.operation, 'ready': bool(predicate(state['editor'])),
            'confirmation_count': self.confirmations, 'snapshot_reads': self.reads,
            'notification_wakes': self.wakes})
        return state if predicate(state['editor']) else None

    def wait(self, predicate, seconds=150):
        self.deadline = time.monotonic() + seconds
        try:
            while True:
                if time.monotonic() >= self.deadline:
                    raise EditorWaitDeadline('Editor transition deadline exceeded')
                try:
                    self.owned_snapshot()
                    ready = self.read_ready()
                    connection = validate_connection(self.changes.read(self.native), self.worker.root)
                except OSError as error:
                    if getattr(error, 'winerror', None) not in (2, 3, 5, 32, 33):
                        raise
                    ready = None
                if ready is not None:
                    serial = (ready['epoch'], ready['sequence'])
                    reloaded = self.before and ready['epoch'] != self.before['epoch']
                    reconnected = connection['session_id'] != self.connection['session_id']
                    if (not ready['unavailable'] and predicate(ready) and
                            serial != self.last_confirmed and
                            (not reloaded or reconnected)):
                        self.last_confirmed = serial
                        state = self.confirm(predicate)
                        if state is not None:
                            return state
                try:
                    self.changes.wait(self.deadline)
                except ImportWaitError as error:
                    if time.monotonic() >= self.deadline:
                        raise EditorWaitDeadline('Editor transition deadline exceeded') from error
                    raise
                self.wakes += 1
        except Exception:
            # No timer-based status retry, reconnect loop, or uncertain Stop.
            self.worker.safe = False
            raise
