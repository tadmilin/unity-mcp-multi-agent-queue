"""One local worker. No mutation replay and no automatic quarantine clearing."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from store import WorkerLock, digest, encode, target_path


class StatusUnavailable(RuntimeError):
    """A read-only observation failed; this never licenses a mutation replay."""


def gate_factory():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'UnityEditorGate'))
    from gate_client import GateClient
    root = Path(__file__).resolve().parents[2]
    # Match the pinned package's ProjectIdentityUtility using Application.dataPath.
    # Every call, including reconnect reads, targets this checkout explicitly.
    instance = root.name + '@' + hashlib.sha1((root / 'Assets').as_posix().encode()).hexdigest()[:16]
    return GateClient(timeout=30, instance=instance)


class Worker:
    def __init__(self, store, root, factory=gate_factory, timeout=900, interval=2):
        self.store, self.root, self.factory = store, Path(root), factory
        self.timeout, self.interval = timeout, interval
        self.client = None

    def rpc(self, job, action, **parameters):
        # Commit intent BEFORE network I/O. Only claim denials and read-only status
        # are retried. A lost mutation response always stops this worker.
        self.store.update(job, phase=action)
        self.store.event(job, 'intent', {'action': action, **parameters})
        try:
            response = self.client.call(action, **parameters)
        except Exception as error:
            self.store.event(job, 'transport_error', {'action': action, 'type': type(error).__name__, 'message': str(error)})
            raise
        if action == 'request' and response.get('success') is True:
            data = response['data']
            self.store.update(job, token=data['token'], ticket=data['ticket'])
            self.client.token = data['token']
        self.store.event(job, 'response', {'action': action, 'response': response})
        return response

    def require(self, response):
        if response.get('success') is not True:
            raise RuntimeError('Gate refused operation; inspect recorded response')
        return response.get('data')

    def status(self, job, owner=False):
        # An unclaimed waiter is not an authenticated holder. Public status must
        # explicitly omit its token. Reads can reconnect safely across reloads.
        token = self.client.token
        try:
            response = self.client.call('status', token=token if owner else None)
        except Exception as error:
            self.store.event(job, 'status_unavailable', {'exception': type(error).__name__})
            response = None
        if not isinstance(response, dict) or response.get('success') is not True:
            self.store.event(job, 'status_unavailable', {'response': response})
            self.client.close()
            try:
                self.client = self.factory()
                self.client.token = token
            except Exception as error:
                self.store.event(job, 'status_reconnect_unavailable', {'exception': type(error).__name__})
            raise StatusUnavailable('Read-only status unavailable; awaiting reconnect')
        data = response['data']
        self.store.event(job, 'status', data)
        return data

    def files_match(self, manifest, after=False):
        for item in manifest['files']:
            path = target_path(self.root, item['path'])
            actual = digest(path.read_bytes()) if path.is_file() else 'missing'
            expected = ('missing' if item.get('delete') else digest(base64.b64decode(item['base64']))) \
                if after else item['expectedSha256']
            if actual != expected:
                raise ValueError('File hash mismatch: ' + item['path'])

    @staticmethod
    def has_error(response, code):
        return isinstance(response, dict) and any(response.get(k) == code for k in ('error', 'message'))

    def wait_status(self, job, owner=False, deadline=None):
        deadline = deadline or time.monotonic() + self.timeout
        while True:
            try:
                return self.status(job, owner=owner)
            except StatusUnavailable:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Read-only status deadline exceeded')
                time.sleep(self.interval)

    def reconcile_claim(self, job, deadline):
        """Observe a lost claim, or revoke its still-unclaimed ticket before replacing it.

        No old claim, write, execute or import request is replayed. A successful
        cancel is an authority-side barrier: a late old claim now has an invalid token.
        """
        while time.monotonic() < deadline:
            try:
                response = self.client.call('status', token=self.client.token)
            except Exception:
                response = {}
            self.store.event(job, 'claim_observation', response)
            if response.get('success') is True:
                data, row = response['data'], self.store.get(job)
                holder = data.get('holder') or {}
                if (holder.get('ticket') != row['ticket'] or holder.get('owner') != 'coordinator-' + job
                    or holder.get('blocked') or data.get('receipt') is not None or data.get('import') is not None):
                    raise RuntimeError('Claim evidence is not an unused owned phase')
                self.store.event(job, 'claim_reconciled', {'ticket': row['ticket'], 'outcome': 'held'})
                return True
            if self.has_error(response, 'EDITOR_QUEUE_TOKEN_INVALID'):
                # Only a still-waiting token can be cancelled. A raced/lost cancel
                # response stays quarantined rather than requesting another token.
                old_ticket = self.store.get(job)['ticket']
                self.require(self.rpc(job, 'cancel'))
                self.store.event(job, 'claim_waiter_retired', {'ticket': old_ticket})
                self.store.update(job, token=None, ticket=None)
                self.client.token = None
                if self.store.control('stop') == '1' or time.monotonic() >= deadline:
                    self.finish(job, 'cancelled', {'reason': 'Unclaimed ticket cancelled'}, True)
                    return None
                self.require(self.rpc(job, 'request', owner='coordinator-' + job, seconds=1800))
                return False
            # Reconnect only the observation channel; never repeat the uncertain claim.
            try:
                self.status(job)
            except StatusUnavailable:
                pass
            time.sleep(self.interval)
        raise TimeoutError('Claim outcome not established before deadline')

    def wait_for_claim(self, job):
        deadline = time.monotonic() + self.timeout
        while True:
            if self.store.control('stop') == '1' or time.monotonic() >= deadline:
                self.require(self.rpc(job, 'cancel'))
                self.finish(job, 'cancelled', {'reason': 'Stopped or wait deadline exceeded'}, True)
                return False
            state = self.wait_status(job, deadline=deadline)
            if state.get('holder') is not None or not state['editor']['idle']:
                # A long synchronous tool blocks Unity's main thread. Poll only
                # reads until the Editor is available, so a read timeout is not
                # confused with an uncertain ownership-changing command.
                time.sleep(self.interval)
                continue
            try:
                response = self.rpc(job, 'claim')
            except (OSError, RuntimeError):
                response = {}
            if response.get('success') is True:
                return True
            if any(self.has_error(response, code) for code in
                   ('EDITOR_QUEUE_HELD', 'EDITOR_QUEUE_FIFO_OR_TOKEN', 'EDITOR_QUEUE_EDITOR_NOT_IDLE')):
                time.sleep(self.interval)
                continue
            outcome = self.reconcile_claim(job, deadline)
            if outcome is True:
                return True
            if outcome is None:
                return False

    def wire_files(self, job, manifest, gate_state):
        if len(encode(manifest['files']).encode()) <= 256 * 1024:
            return manifest['files']
        if gate_state.get('stagedFileVersion') != 'sha256-v1':
            raise ValueError('Large payload requires gate stagedFileVersion=sha256-v1')
        folder = self.root / 'JobQueue/payloads' / job
        for parent in [folder, *folder.parents]:
            if parent == self.root:
                break
            if parent.is_symlink() or (parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0) & 1024):
                raise ValueError('Staging reparse point refused')
        folder.mkdir(parents=True, exist_ok=True)
        files = []
        for item in manifest['files']:
            if item.get('delete'):
                files.append(item)
                continue
            content = base64.b64decode(item['base64'])
            checksum = digest(content)
            path = folder / (checksum + '.bin')
            if path.exists():
                if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 1024:
                    raise ValueError('Staging reparse point refused')
                if digest(path.read_bytes()) != checksum:
                    raise ValueError('Existing staged bytes changed')
            else:
                with path.open('xb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            files.append({'path': item['path'], 'expectedSha256': item['expectedSha256'],
                          'stagedPath': path.relative_to(self.root).as_posix(), 'stagedSha256': checksum})
        return files

    def finish(self, job, state, result, clear_token=False):
        fields = dict(state=state, result=encode(result), finished=time.time(), phase='done')
        if clear_token:
            fields['token'] = None
        self.store.update(job, **fields)

    def process(self, job):
        row = self.store.get(job)
        manifest = json.loads(row['manifest'])
        attempted_request = False
        try:
            if digest(encode(manifest).encode()) != row['digest']:
                raise ValueError('Stored job payload digest mismatch')
            self.files_match(manifest)
            self.client = self.factory()
            state = self.wait_status(job)
            if (state.get('persistenceVersion') != 'fail-closed-v2' or
                    state.get('importScheduler') != 'synchronous-refresh-v3'):
                raise ValueError('Required gate version not loaded')
            files = self.wire_files(job, manifest, state)
            attempted_request = True
            self.require(self.rpc(job, 'request', owner='coordinator-' + job, seconds=1800))
            if not self.wait_for_claim(job):
                return
            self.store.update(job, claimed=time.time())
            self.wait_status(job, owner=True)
            operation = job + '-write'
            if manifest['kind'] == 'files':
                data = self.require(self.rpc(job, 'write_files', operation=operation, files=files))
                self.require(data['result'])
                self.require(self.rpc(job, 'ack', operation=operation))
                self.require(self.rpc(job, 'import'))
                deadline, renewed = time.monotonic() + self.timeout, time.monotonic()
                while True:
                    try:
                        state = self.status(job, owner=True)
                    except StatusUnavailable:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Import status unavailable')
                        time.sleep(self.interval)
                        continue
                    current = state.get('import') or {}
                    if current.get('operation') != operation:
                        raise RuntimeError('Import receipt does not match this job')
                    if current.get('state') == 'complete' and state['editor']['idle']:
                        break
                    if current.get('state') not in ('scheduled', 'running', 'settling', 'reloading'):
                        raise RuntimeError('Import requires operator reconciliation')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Import deadline exceeded; do not replay')
                    if time.monotonic() - renewed > 60:
                        self.require(self.rpc(job, 'renew'))
                        renewed = time.monotonic()
                    time.sleep(self.interval)
                self.files_match(manifest, after=True)
            # Capture console without clearing it. Compilation acceptance is not a
            # gameplay test, and existing console errors are preserved in results.
            operation = job + '-console'
            data = self.require(self.rpc(job, 'execute', operation=operation,
                command='read_console', parameters={'action': 'get', 'types': ['error'], 'count': 20}))
            console = data['result']
            self.require(self.rpc(job, 'ack', operation=operation))
            state = self.wait_status(job, owner=True)
            if not state['editor']['idle']:
                raise RuntimeError('Editor not idle at completion')
            self.require(self.rpc(job, 'release'))
            self.finish(job, 'succeeded' if console.get('success') is True else 'failed',
                {'scope': 'gate/import/hash checks only; no gameplay assertions',
                 'console': console, 'editor_before_release': state['editor']}, True)
        except Exception as ex:
            # Preserve raw credentials in SQLite for the owner, never in reports.
            # Even a request may have reached Unity without returning its token.
            self.store.update(job, state='quarantined' if attempted_request else 'failed',
                result=encode({'reason': type(ex).__name__ + ': ' + str(ex)}),
                finished=time.time())
        finally:
            if self.client:
                self.client.close()
                self.client = None

    def run(self, once=False):
        with WorkerLock(self.store.home):
            self.store.interrupted()
            self.store.control('stop', '0')
            while self.store.control('stop') != '1':
                if self.store.db.execute("SELECT 1 FROM jobs WHERE state='quarantined'").fetchone():
                    return 2
                job = self.store.next_job()
                if job:
                    self.process(job)
                if once:
                    return 2 if job and self.store.get(job)['state'] == 'quarantined' else 0
                if not job:
                    time.sleep(self.interval)
            return 0
