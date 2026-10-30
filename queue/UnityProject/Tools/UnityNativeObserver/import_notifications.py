"""Publish a local reconnect hint from the actual native registration event.

This file grants no authority. The checked worker must confirm its own receipt,
operation, boot and clean Editor through MCP before continuing. No polling.
"""
from hashlib import sha1
import json
import os
from pathlib import Path
import time
import uuid

VERSION = 'native-import-notification-v1'
ROOT = Path(__file__).resolve().parents[2]
PROJECT_ID = sha1((ROOT / 'Assets').as_posix().encode()).hexdigest()[:16]
EPOCH = uuid.uuid4().hex


def write_snapshot(target, value):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / (uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == 'nt' and target.exists():
            # MoveFileEx (os.replace) can reject an open shared reader on Windows.
            # ReplaceFile matches the gate's durable File.Replace operation.
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.ReplaceFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
            api.ReplaceFileW.restype = wintypes.BOOL
            if not api.ReplaceFileW(str(target), str(temporary), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def publish_connected(project_hash, project_path, session_id):
    if (project_hash != PROJECT_ID or not project_path or
            Path(project_path).resolve() != ROOT.resolve()):
        return
    write_snapshot(ROOT / 'Library/UnityWorkflowSignals/native-connected.json', {
        'schema': VERSION, 'workspace': str(ROOT), 'project_id': PROJECT_ID,
        'native_pid': os.getpid(), 'epoch': EPOCH, 'session_id': session_id,
        'connected_unix': time.time()})
