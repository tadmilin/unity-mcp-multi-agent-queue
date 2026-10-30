"""Pre-claim OS notifications. Hints never grant ownership or confirm a task.

The unchanged Editor authority decides FIFO/readiness on claim. No Unity RPC is
sent while the durable queue has a holder/earlier waiter or the Editor hint is
busy. A fixed monotonic deadline is capped by the original waiting ticket lease.
"""
from hashlib import sha256
import json
from pathlib import Path
import time

from import_wait import DirectoryChanges, ImportWaitError

VERSION = 'preclaim-notification-v1'
LOADED_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()


class ClaimWaitError(RuntimeError):
    pass


class ClaimWait:
    def __init__(self, worker, job, initial, changes=DirectoryChanges, deadline=None):
        self.worker, self.job, self.initial = worker, job, initial
        self.gate = worker.root / 'Library/EditorQueueGate/state.json'
        self.ready = worker.root / 'Library/UnityWorkflowSignals/editor-readiness.json'
        self.changes = changes([self.gate.parent, self.ready.parent, worker.store.home])
        self.ticket = worker.store.get(job)['ticket']
        self.owner = 'coordinator-' + job
        self.secret = sha256(worker.client.token.encode()).hexdigest()
        self.deadline = time.monotonic() + worker.timeout
        if deadline is not None:
            self.deadline = min(self.deadline, deadline)
        self.waiter_deadline = None
        self.wakes, self.reads, self.claims = 0, 0, 0
        self.last_candidate = None
        self.uncertain = False
        self.foreign = False

    def snapshot(self):
        state = self.changes.read(self.gate)
        self.reads += 1
        if state.get('version') != 1 or not isinstance(state.get('waiting'), list):
            raise ClaimWaitError('Invalid gate hint; no claim')
        own = [w for w in state['waiting'] if w.get('ticket') == self.ticket]
        if (len(own) != 1 or own[0].get('owner') != self.owner or
                own[0].get('secretHash') != self.secret):
            raise ClaimWaitError('Waiting identity changed; no claim')
        lease = own[0].get('deadline')
        if not isinstance(lease, (float, int)) or isinstance(lease, bool):
            raise ClaimWaitError('Invalid waiting deadline')
        if self.waiter_deadline is None:
            self.waiter_deadline = lease
            self.deadline = min(self.deadline, time.monotonic() + lease - time.time())
        elif lease != self.waiter_deadline:
            raise ClaimWaitError('Waiting lease changed; no deadline extension')
        earlier = state['waiting'][:state['waiting'].index(own[0])]
        # Expiry is only a wake hint. Claim lets the authority sweep expired
        # waiters; this observer never edits the queue or frees a held phase.
        live_earlier = [w for w in earlier if w['deadline'] > time.time()]
        expiry = min([w['deadline'] for w in live_earlier], default=None)
        next_wake = min(self.deadline, time.monotonic() + expiry - time.time()) if expiry else self.deadline
        if state.get('holder') is not None or live_earlier:
            return None, next_wake
        current_import = state.get('import')
        if current_import is not None and current_import.get('state') != 'complete':
            return None, self.deadline
        signal = self.changes.read(self.ready)
        if (signal.get('schema') != VERSION or
                Path(signal.get('workspace', '')).resolve() != self.worker.root.resolve() or
                not signal.get('epoch') or type(signal.get('sequence')) is not int):
            raise ClaimWaitError('Readiness identity unavailable or mismatched')
        if signal.get('pid') != self.initial['editor']['pid']:
            # A foreign or stale publisher (AssetImportWorker child, dead/restarted
            # Editor; 28 Oct 2026) is no hint at all. The live authority decides:
            # at most one claim per gate change, and rewrites by the foreign pid
            # never add RPCs. The Editor's own next transition wakes us normally.
            if not self.foreign:
                self.foreign = True
                self.worker.store.event(self.job, 'claim_wait_foreign_hint', {
                    'ticket': self.ticket, 'hint_pid': signal.get('pid'),
                    'editor_pid': self.initial['editor']['pid']})
            return (self.ticket, 'foreign-hint', self.gate.stat().st_mtime_ns), self.deadline
        if signal.get('idle') is not True:
            return None, self.deadline
        return (self.ticket, signal['epoch'], signal['sequence'],
                self.gate.stat().st_mtime_ns), self.deadline

    def cancel(self, reason, state='cancelled'):
        self.worker.require(self.worker.rpc(self.job, 'cancel'))
        self.worker.store.event(self.job, 'claim_wait_cancelled', {
            'ticket': self.ticket, 'reason': reason, 'notification_wakes': self.wakes,
            'snapshot_reads': self.reads, 'claim_attempts': self.claims})
        self.worker.finish(self.job, state, {'reason': reason}, True)
        return False

    def attempt(self):
        worker = self.worker
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return False
        transport = worker.client.client
        old_timeout = transport.timeout
        self.claims += 1
        self.uncertain = True
        try:
            transport.timeout = min(old_timeout, remaining)
            response = worker.rpc(self.job, 'claim')
        except Exception:
            response = None
        finally:
            transport.timeout = old_timeout
        if isinstance(response, dict) and response.get('success') is True:
            holder = response.get('data') or {}
            if holder.get('ticket') != self.ticket or holder.get('owner') != self.owner or holder.get('blocked'):
                raise ClaimWaitError('Claim returned mismatched ownership')
            self.uncertain = False
            return True
        if any(worker.has_error(response, code) for code in
               ('EDITOR_QUEUE_HELD', 'EDITOR_QUEUE_FIFO_OR_TOKEN', 'EDITOR_QUEUE_EDITOR_NOT_IDLE')):
            self.uncertain = False
            return False
        # A lost mutation is never retried or replaced. One bounded authenticated
        # receipt/ownership observation may prove it succeeded; otherwise retain.
        remaining = self.deadline - time.monotonic()
        if remaining > 0:
            try:
                transport.timeout = min(old_timeout, remaining)
                observed = worker.client.call('status', token=worker.client.token)
                worker.store.event(self.job, 'claim_observation', observed)
            finally:
                transport.timeout = old_timeout
            if observed.get('success') is True:
                state = observed['data']
                holder = state.get('holder') or {}
                if (holder.get('ticket') == self.ticket and holder.get('owner') == self.owner
                        and not holder.get('blocked') and state.get('receipt') is None
                        and state.get('import') is None):
                    self.uncertain = False
                    return True
        raise ClaimWaitError('Claim outcome uncertain; no replay, cancel or replacement')

    def run(self):
        worker = self.worker
        worker.store.event(self.job, 'claim_wait_armed', {'version': VERSION, 'ticket': self.ticket})
        try:
            while True:
                if worker.store.control('stop') == '1' or time.monotonic() >= self.deadline:
                    return self.cancel('Stopped or wait deadline exceeded')
                try:
                    candidate, next_wake = self.snapshot()
                except OSError as error:
                    if getattr(error, 'winerror', None) not in (2, 3, 5, 32, 33):
                        raise
                    candidate, next_wake = None, self.deadline
                if candidate is not None and candidate != self.last_candidate:
                    self.last_candidate = candidate
                    if self.attempt():
                        worker.store.event(self.job, 'claim_wait_confirmed', {
                            'ticket': self.ticket, 'claim_attempts': self.claims,
                            'notification_wakes': self.wakes, 'snapshot_reads': self.reads,
                            'authority': 'fresh MCP claim or matching authenticated reconciliation'})
                        return True
                try:
                    self.changes.wait(next_wake)
                    self.wakes += 1
                except ImportWaitError:
                    # A fixed lease/queue deadline wake; never a status timer.
                    if time.monotonic() < next_wake:
                        raise
        except Exception as error:
            if self.uncertain:
                worker.safe = False
                raise
            return self.cancel(type(error).__name__ + ': ' + str(error), 'failed')
        finally:
            self.changes.close()
