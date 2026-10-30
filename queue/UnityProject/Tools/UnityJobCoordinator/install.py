"""Install this workspace's local queue without downloads, admin, or startup tasks."""
import datetime as dt
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from store import Store, WorkerLock, digest


def install(root, python=None):
    root = Path(root).resolve()
    python = python or sys.executable
    home = root / 'JobQueue'
    source = root / 'Tools/UnityJobCoordinator'
    required = [source / name for name in ('cli.py', 'store.py', 'worker.py', 'install.py')]
    required += [root / 'Tools/UnityEditorGate/gate_client.py', root / 'Tools/UnityReloadDiagnostics/mcp_cli.py']
    if sys.version_info < (3, 10) or any(not p.is_file() for p in required):
        raise RuntimeError('Requires Python 3.10+ and the existing Unity gate tooling')
    if '%' in str(root) or '%' in str(python):
        raise ValueError('Batch launcher paths cannot contain a percent sign')
    home.mkdir(exist_ok=True)
    with WorkerLock(home):
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = root / 'Backups/UnityJobCoordinatorInstall' / stamp
        old = [home / name for name in ('queue.sqlite3', 'queue.cmd', 'Start.cmd', 'Status.cmd', 'Stop.cmd', 'installed.json')]
        if any(p.exists() for p in old):
            backup.mkdir(parents=True)
            for path in old:
                if not path.exists():
                    continue
                if path.suffix == '.sqlite3':
                    original = sqlite3.connect(path)
                    copy = sqlite3.connect(backup / path.name)
                    try:
                        original.backup(copy)
                    finally:
                        copy.close()
                        original.close()
                else:
                    shutil.copy2(path, backup / path.name)
        store = Store(home)
        store.close()
        launch = f'@echo off\r\n"{python}" "{source / "cli.py"}" %*\r\nexit /b %errorlevel%\r\n'
        (home / 'queue.cmd').write_bytes(launch.encode('utf-8'))
        for name, command in [('Start', 'start'), ('Status', 'status'), ('Stop', 'stop')]:
            (home / (name + '.cmd')).write_bytes(
                ('@echo off\r\ncall "%~dp0queue.cmd" ' + command + '\r\npause\r\n').encode('ascii'))
        metadata = {'schema': 1, 'installed_utc': stamp, 'python': python,
                    'files': {str(p.relative_to(root)): digest(p.read_bytes()) for p in required},
                    'backup': str(backup.relative_to(root)) if backup.exists() else None}
        (home / 'installed.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    return {'installed': str(home), 'worker_started': False, 'command': r'.\JobQueue\queue.cmd start'}


if __name__ == '__main__':
    try:
        print(json.dumps(install(Path(__file__).resolve().parents[2]), indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        raise SystemExit(1)
