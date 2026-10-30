"""Event wait outside Unity transport; files are hints, never acceptance.

Windows directory notification handles stay armed before import. Native plugin
registration survives domain reload and wakes the existing service worker. Gate
files are read with FILE_SHARE_DELETE so observation cannot block atomic saves.
No timer polling, subprocess, retry of mutations, or changes to the frozen gate.
"""
import ctypes
from ctypes import wintypes
from hashlib import sha1, sha256
import json
import math
import os
from pathlib import Path
import re
import time

VERSION = 'checked-import-notification-v2-preserved-session'
_BOOT = re.compile(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,7}))?Z')
LOADED_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()


class ImportWaitError(RuntimeError):
    pass


class DirectoryChanges:
    def __init__(self, folders):
        if os.name != 'nt':
            raise ImportWaitError('Import notifications require Windows; no polling fallback')
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        api = self.api
        api.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
        api.FindFirstChangeNotificationW.restype = wintypes.HANDLE
        api.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
        api.FindNextChangeNotification.restype = wintypes.BOOL
        api.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
        api.FindCloseChangeNotification.restype = wintypes.BOOL
        api.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
        api.WaitForMultipleObjects.restype = wintypes.DWORD
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handles = []
        try:
            for folder in folders:
                handle = api.FindFirstChangeNotificationW(str(Path(folder).resolve()), False, 0x11)
                if handle in (None, ctypes.c_void_p(-1).value):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles.append(handle)
        except BaseException:
            self.close()
            raise

    def read(self, path):
        import msvcrt
        # GENERIC_READ, share read/write/delete, OPEN_EXISTING.
        handle = self.api.CreateFileW(str(path), 0x80000000, 7, None, 3, 0, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            self.api.CloseHandle(handle)
            raise
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ImportWaitError('Notification snapshot exceeds limit')
        return json.loads(raw.decode('utf-8-sig'))

    def wait(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ImportWaitError('Import notification deadline exceeded; no replay')
        handles = (wintypes.HANDLE * len(self.handles))(*self.handles)
        result = self.api.WaitForMultipleObjects(len(handles), handles, False, math.ceil(remaining * 1000))
        if result == 258:
            raise ImportWaitError('Import notification deadline exceeded; no replay')
        if result >= len(handles):
            raise ctypes.WinError(ctypes.get_last_error())
        # Rearm before reading either snapshot, avoiding a read/rearm lost wake.
        if not self.api.FindNextChangeNotification(self.handles[result]):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        for handle in self.handles:
            self.api.FindCloseChangeNotification(handle)
        self.handles.clear()


def boot_id(value):
    # The gate writes BootUtc "o" (7 digits) to state.json, but the MCP transport
    # re-serializes dates and drops trailing zeros (".7592380Z" vs ".759238Z",
    # 28 Oct 2026 ticket736). Compare the canonical 7-digit form; anything else fails.
    match = _BOOT.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ImportWaitError('Boot evidence is not a UTC timestamp; no replay')
    return match.group(1) + '.' + (match.group(2) or '').ljust(7, '0') + 'Z'


def validate_connection(value, root):
    project = sha1((root / 'Assets').as_posix().encode()).hexdigest()[:16]
    if (value.get('schema') != 'native-import-notification-v1' or
            Path(value.get('workspace', '')).resolve() != root.resolve() or
            value.get('project_id') != project or not value.get('epoch') or
            not value.get('session_id') or not isinstance(value.get('native_pid'), int)):
        raise ImportWaitError('Native notification identity unavailable or mismatched')
    return value


class ImportWait:
    def __init__(self, worker, job, operation, initial, changes=DirectoryChanges):
        self.worker, self.job, self.operation = worker, job, operation
        self.root, self.initial = worker.root, initial
        self.gate_path = self.root / 'Library/EditorQueueGate/state.json'
        self.native_path = self.root / 'Library/UnityWorkflowSignals/native-connected.json'
        self.changes = changes([self.gate_path.parent, self.native_path.parent])
        self.reads, self.wakes = 0, 0
        self.receipt_hash = None
        try:
            self.connection = validate_connection(self.changes.read(self.native_path), self.root)
            row = worker.store.get(job)
            self.ticket = row['ticket']
            self.secret_hash = sha256(worker.client.token.encode()).hexdigest()
            self.boot = initial['bootUtc']
            self.boot_id = boot_id(self.boot)
            self.owner = 'coordinator-' + job
            worker.store.event(job, 'import_wait_armed', {
                'version': VERSION, 'operation': operation, 'ticket': self.ticket,
                'bootUtc': self.boot, 'native_pid': self.connection['native_pid'],
                'session_id': self.connection['session_id']})
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.changes.close()

    def snapshot(self):
        data = self.changes.read(self.gate_path)
        self.reads += 1
        holder = data.get('holder') or {}
        receipt = holder.get('operation') or {}
        current = data.get('import') or {}
        if (data.get('version') != 1 or holder.get('ticket') != self.ticket or
                holder.get('owner') != self.owner or holder.get('secretHash') != self.secret_hash or
                holder.get('blocked') or not isinstance(holder.get('deadline'), (int, float)) or
                holder['deadline'] <= time.time() or receipt.get('id') != self.operation or
                receipt.get('state') != 'acknowledged' or
                (receipt.get('result') or {}).get('success') is not True or
                not receipt.get('sha256') or current.get('operation') != self.operation or
                boot_id(current.get('originBootUtc')) != self.boot_id or
                not isinstance(current.get('requiresReload'), bool)):
            raise ImportWaitError('Import ownership/receipt/boot evidence changed; no replay')
        if self.receipt_hash is not None and (receipt['sha256'] != self.receipt_hash or
                current['requiresReload'] != self.reload):
            raise ImportWaitError('Import receipt identity changed; no replay')
        return holder, current

    def staged(self):
        holder, current = self.snapshot()
        if current['state'] != 'staged':
            raise ImportWaitError('Import is not this job staged write')
        self.receipt_hash = holder['operation']['sha256']
        self.reload = current['requiresReload']
        # Retain the declared import timeout; never outlive the existing lease.
        self.deadline = min(time.monotonic() + self.worker.timeout,
                            time.monotonic() + holder['deadline'] - time.time())

    def wait(self):
        while True:
            if time.monotonic() >= self.deadline:
                raise ImportWaitError('Import notification deadline exceeded; no replay')
            try:
                holder, current = self.snapshot()
                connection = validate_connection(self.changes.read(self.native_path), self.root)
            except OSError as error:
                if getattr(error, 'winerror', None) not in (2, 3, 5, 32, 33):
                    raise
                # ReplaceFile may briefly remove the name. Wait for its next
                # rename/close notification; never retry from a timer.
                self.changes.wait(self.deadline)
                self.wakes += 1
                continue
            if current['state'] not in ('scheduled', 'running', 'settling', 'reloading', 'complete'):
                raise ImportWaitError('Import requires operator reconciliation: ' + str(current['state']))
            if (connection['epoch'], connection['native_pid']) != (self.connection['epoch'], self.connection['native_pid']):
                raise ImportWaitError('Native process changed during import; no replay')
            reconnected = connection['session_id'] != self.connection['session_id']
            # Domain reload need not replace the native transport session. A
            # complete gate snapshot can wake one live authority confirmation;
            # it is never acceptance. confirm() still requires a changed Unity
            # boot for script imports and verifies receipt, owner, PID and idle.
            if (current['state'] == 'complete' or
                    (self.reload and reconnected and current['state'] == 'reloading')):
                self.worker.store.event(self.job, 'import_wait_notified', {
                    'operation': self.operation, 'ticket': self.ticket, 'requires_reload': self.reload,
                    'state': current['state'], 'notification_wakes': self.wakes, 'snapshot_reads': self.reads,
                    'session_changed': reconnected})
                return self.confirm()
            # Only an OS change event or the fixed deadline can resume this wait.
            self.changes.wait(self.deadline)
            self.wakes += 1

    def confirm(self):
        worker = self.worker
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ImportWaitError('Import confirmation deadline exceeded')
        transport = worker.client.client
        previous_timeout = transport.timeout
        worker.store.event(self.job, 'import_confirmation_intent', {'operation': self.operation})
        try:
            transport.timeout = min(previous_timeout, remaining)
            response = worker.client.call('status', token=worker.client.token)
        finally:
            transport.timeout = previous_timeout
        if not isinstance(response, dict) or response.get('success') is not True:
            worker.store.event(self.job, 'import_confirmation_unavailable', {'operation': self.operation})
            raise ImportWaitError('Import confirmation unavailable; no polling or replay')
        state = response['data']
        worker.store.event(self.job, 'status', state)
        holder, receipt, current = state.get('holder') or {}, state.get('receipt') or {}, state.get('import') or {}
        if (time.monotonic() >= self.deadline or holder.get('ticket') != self.ticket or
                holder.get('owner') != self.owner or holder.get('blocked') or
                holder.get('deadline', 0) <= time.time() or
                receipt.get('id') != self.operation or receipt.get('state') != 'acknowledged' or
                receipt.get('sha256') != self.receipt_hash or
                (receipt.get('result') or {}).get('success') is not True or
                current.get('operation') != self.operation or boot_id(current.get('originBootUtc')) != self.boot_id or
                current.get('requiresReload') != self.reload or current.get('state') != 'complete' or
                not state.get('editor', {}).get('idle') or not state.get('bootUtc') or
                (self.reload and boot_id(state['bootUtc']) == self.boot_id) or
                state.get('editor', {}).get('pid') != self.initial.get('editor', {}).get('pid')):
            raise ImportWaitError('Live import confirmation mismatch; no replay or cleanup')
        worker.store.event(self.job, 'import_wait_confirmed', {'operation': self.operation,
                           'ticket': self.ticket, 'bootUtc': state['bootUtc'], 'status_rpc_count': 1})
        return state
