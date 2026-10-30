"""Await a pinned Windows process lifetime without polling or subprocesses."""
import asyncio
import ctypes
from ctypes import wintypes
import os


class ServiceExited(RuntimeError):
    pass


def _api():
    if os.name != 'nt':
        raise RuntimeError('Process lifetime notifications require Windows')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        'GetProcessTimes': ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        'CreateEventW': ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        'SetEvent': ([wintypes.HANDLE], wintypes.BOOL),
        'WaitForMultipleObjects': ([wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD], wintypes.DWORD),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        getattr(api, name).argtypes = args
        getattr(api, name).restype = result
    return api


def _born(api, handle):
    times = [wintypes.FILETIME() for _ in range(4)]
    if not api.GetProcessTimes(handle, *(ctypes.byref(x) for x in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


def process_started_ticks(pid=None):
    api = _api()
    handle = api.OpenProcess(0x1000, False, os.getpid() if pid is None else pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _born(api, handle)
    finally:
        api.CloseHandle(handle)


class ProcessWatch:
    def __init__(self, pid, started_ticks):
        if type(pid) is not int or not 0 < pid <= 0xffffffff or type(started_ticks) is not int:
            raise ValueError('Missing service lifetime identity; no workload sent')
        self.api = _api()
        self.process = self.api.OpenProcess(0x100000 | 0x1000, False, pid)
        self.cancel = None
        if not self.process:
            raise ServiceExited('Central service exited before workload dispatch')
        try:
            if _born(self.api, self.process) != started_ticks:
                raise ServiceExited('Central service PID lifetime changed before dispatch')
            self.cancel = self.api.CreateEventW(None, True, False, None)
            if not self.cancel:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def wait(self):
        handles = (wintypes.HANDLE * 2)(self.process, self.cancel)
        result = self.api.WaitForMultipleObjects(2, handles, False, 0xffffffff)
        if result not in (0, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def stop(self):
        if not self.api.SetEvent(self.cancel):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        for name in ('cancel', 'process'):
            handle = getattr(self, name, None)
            if handle:
                self.api.CloseHandle(handle)
                setattr(self, name, None)


async def guarded_workload(client, tool, arguments, capabilities):
    """One RPC, raced against exact service exit; never replay or auto-submit."""
    watch = ProcessWatch(capabilities.get('service_pid'), capabilities.get('service_started_ticks'))
    watcher = asyncio.get_running_loop().run_in_executor(None, watch.wait)
    request = asyncio.create_task(client.call_tool(tool, arguments, raise_on_error=False))
    try:
        done, _ = await asyncio.wait((request, watcher), return_when=asyncio.FIRST_COMPLETED)
        if request in done:
            return request.result()
        if watcher.result():
            raise ServiceExited('Central service process exited; recover the same mode/key, never replay')
        raise RuntimeError('Service lifetime watch stopped unexpectedly')
    finally:
        watch.stop()
        if not request.done():
            request.cancel()
        try:
            await asyncio.gather(request, return_exceptions=True)
            await asyncio.shield(watcher)
        finally:
            watch.close()
