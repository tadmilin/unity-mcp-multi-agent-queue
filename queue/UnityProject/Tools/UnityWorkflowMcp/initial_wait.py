"""Pre-request readiness hints; no Unity RPC, ownership or task acceptance.

Arm directory notifications before reading. A live publisher can be busy: enqueue
without waiting for idle so the authority retains FIFO admission. Reload/absent
publisher signals wait on events within the same queue deadline used by claim.
"""
import ctypes
from ctypes import wintypes
from hashlib import sha256
from pathlib import Path
import time

from claim_wait import VERSION as READINESS_SCHEMA
from import_wait import DirectoryChanges, ImportWaitError

VERSION = 'initial-readiness-editor-identity-v2'
LOADED_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()


class InitialReadinessError(RuntimeError):
    pass


def command_line_arguments(command):
    """Use Windows quoting rules; never log launch arguments (may contain tokens)."""
    if not isinstance(command, str) or not command.strip() or '\x00' in command:
        return []
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    values = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not values:
        return []
    try:
        return [values[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(values)


def editor_command(image, command, workspace=None):
    """Positive Editor layout/project evidence; workers share the same executable."""
    path = Path(image)
    if (path.name.lower() != 'unity.exe' or
            not (path.parent / 'Data/Managed/UnityEditor.dll').is_file()):
        return False
    args = command_line_arguments(command)
    if not args or Path(args[0]).resolve() != path.resolve():
        return False
    projects = []
    for index, arg in enumerate(args[1:], 1):
        key, equals, value = arg.partition('=')
        key = key.lower()
        if key in ('-adb2', '-batchmode'):
            return False
        if key in ('-name', '-projectpath'):
            if not equals:
                value = args[index + 1] if index + 1 < len(args) else ''
            if key == '-name' and value.lower().startswith('assetimportworker'):
                return False
            if key == '-projectpath':
                projects.append(value)
    if len(projects) != 1 or not Path(projects[0]).is_absolute():
        return False
    return workspace is None or Path(projects[0]).resolve() == Path(workspace).resolve()


def process_command_line(pid):
    """One in-process WMI read using the existing hash-pinned pywin32 runtime."""
    import pythoncom
    import win32com.client

    def query():
        # Release every COM reference before uninitializing this worker thread.
        service = win32com.client.GetObject('winmgmts:root/cimv2')
        process = service.Get('Win32_Process.Handle="%d"' % pid)
        return process.CommandLine

    pythoncom.CoInitialize()
    try:
        return query()
    except pythoncom.com_error:
        return None
    finally:
        pythoncom.CoUninitialize()


def live_unity_process(pid, written_ns, workspace=None):
    """Reject helpers, foreign projects and dead/reused PIDs without Unity RPC."""
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        return False
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.QueryFullProcessImageNameW.restype = wintypes.BOOL
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.GetProcessTimes.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        return False
    try:
        if api.WaitForSingleObject(handle, 0) != 258:
            return False
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        if not api.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            return False
        if Path(image.value).name.lower() != 'unity.exe':
            return False
        times = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return False
        born_ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        born_ns = (born_ticks - 116444736000000000) * 100
        if not 0 < born_ns <= written_ns:
            return False
        if not editor_command(image.value, process_command_line(pid), workspace):
            return False
        # Keep the original handle open through WMI and reject exit during the read.
        return api.WaitForSingleObject(handle, 0) == 258
    finally:
        api.CloseHandle(handle)


class InitialReadinessWait:
    def __init__(self, worker, job, deadline, changes=DirectoryChanges,
                 process_check=live_unity_process):
        self.worker, self.job, self.deadline = worker, job, deadline
        self.path = worker.root / 'Library/UnityWorkflowSignals/editor-readiness.json'
        self.changes = changes([self.path.parent, worker.store.home])
        self.process_check = process_check
        self.reads, self.wakes = 0, 0

    def snapshot(self):
        written_ns = self.path.stat().st_mtime_ns
        value = self.changes.read(self.path)
        self.reads += 1
        if self.path.stat().st_mtime_ns != written_ns:
            return None
        if (not isinstance(value, dict) or value.get('schema') != READINESS_SCHEMA or
                Path(value.get('workspace', '')).resolve() != self.worker.root.resolve() or
                type(value.get('pid')) is not int or not 0 < value['pid'] <= 0xffffffff or
                not isinstance(value.get('epoch'), str) or not value['epoch'] or
                type(value.get('sequence')) is not int or value['sequence'] < 1 or
                any(type(value.get(flag)) is not bool for flag in
                    ('idle', 'unavailable', 'dirty', 'playing', 'changing', 'compiling', 'updating'))):
            raise InitialReadinessError('Readiness publisher identity unavailable or mismatched')
        if value['unavailable'] or not self.process_check(value['pid'], written_ns, self.worker.root):
            return None
        # Busy/dirty/Play is not a reason to postpone taking a FIFO ticket.
        # The request and claim remain live authority calls, never file decisions.
        return value

    def run(self):
        worker = self.worker
        try:
            worker.store.event(self.job, 'initial_readiness_armed', {'version': VERSION})
            while True:
                if worker.store.control('stop') == '1':
                    raise InitialReadinessError('Stopped before request; no ticket created')
                if time.monotonic() >= self.deadline:
                    raise InitialReadinessError('Queue deadline exceeded before request; no ticket created')
                try:
                    value = self.snapshot()
                except OSError as error:
                    if getattr(error, 'winerror', None) not in (2, 3, 5, 32, 33):
                        raise
                    value = None
                if value is not None:
                    worker.store.event(self.job, 'initial_readiness_ready', {
                        'version': VERSION, 'pid': value['pid'], 'epoch': value['epoch'],
                        'sequence': value['sequence'], 'idle_hint': value['idle'],
                        'snapshot_reads': self.reads, 'notification_wakes': self.wakes,
                        'authority': 'hint only; request/claim and completion still require MCP'})
                    return {'editor': {'pid': value['pid']}}
                try:
                    self.changes.wait(self.deadline)
                    self.wakes += 1
                except ImportWaitError:
                    if time.monotonic() < self.deadline:
                        raise
        finally:
            self.changes.close()
