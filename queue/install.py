"""Install the research preview into a closed, isolated Windows Unity project."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
PACKAGE = 'com.coplaydev.unity-mcp'
STATE = '.unity-mcp-queue'
INSTALL_LOCK = Path('queue/requirements-install-lock.txt')
IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo')


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def safe_path(root, path):
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'Path escapes project: {path}')
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or (item.exists() and item.lstat().st_file_attributes & 1024):
            raise ValueError(f'Reparse points are not supported: {item}')


def payload(repo):
    overlay = repo / 'queue/UnityProject'
    items = [(repo / 'MCPForUnity', Path('Packages') / PACKAGE)]
    items += [(p, p.relative_to(overlay)) for p in sorted((overlay / 'Tools').iterdir()) if p.is_dir()]
    items += [(p, p.relative_to(overlay)) for p in sorted((overlay / 'Assets/Editor').iterdir())]
    return items


def preflight(project, repo=REPO, owned_state=False):
    root = Path(project).absolute()
    if root.is_symlink() or (root.exists() and root.lstat().st_file_attributes & 1024):
        raise ValueError('Project root must not be a link or junction')
    root = root.resolve()
    for name in ('Assets', 'Packages/manifest.json', 'ProjectSettings/ProjectVersion.txt'):
        path = root / name
        safe_path(root, path)
        if not path.exists():
            raise ValueError(f'Not a Unity project: missing {name}')
    safe_path(root, root / 'Temp/UnityLockfile')
    if (root / 'Temp/UnityLockfile').exists():
        raise ValueError('Unity lock exists. Close this project before installation; do not remove a live lock.')
    manifest = json.loads((root / 'Packages/manifest.json').read_text(encoding='utf-8-sig'))
    if not isinstance(manifest, dict) or not isinstance(manifest.get('dependencies'), dict):
        raise ValueError('Packages/manifest.json must contain a dependencies object')
    if PACKAGE in manifest['dependencies']:
        raise ValueError('MCP for Unity is already installed. Use a fresh isolated test project; upgrades are not supported.')
    targets = [relative for _, relative in payload(repo)] + [Path('JobQueue'), Path(STATE)]
    for relative in targets:
        target = root / relative
        safe_path(root, target)
        if target.exists() and not (owned_state and relative == Path(STATE)):
            raise ValueError(f'Refusing to overwrite {relative}. Existing or partial installs require manual review.')
    if '%' in str(root) or '!' in str(root) or '\n' in str(root) or '\r' in str(root):
        raise ValueError('Project path cannot contain %, ! or newlines (Windows launchers)')
    return root, manifest


def prepare_environment(state, lock):
    env = state / 'venv'
    subprocess.run([sys.executable, '-I', '-m', 'venv', str(env)], check=True)
    python = env / 'Scripts/python.exe'
    subprocess.run([str(python), '-I', '-m', 'pip', '--isolated', 'install',
                    '--require-hashes', '--only-binary=:all:', '--no-cache-dir',
                    '-r', str(lock)], check=True)
    subprocess.run([str(python), '-I', '-m', 'pip', '--isolated', 'check'], check=True)
    subprocess.run([str(python), '-I', '-c',
                    'import fastmcp, mcp, fastapi, tomli, uvicorn, httpx, pydantic, win32event'], check=True)
    return python


def install(project, repo=REPO):
    root, manifest = preflight(project, repo)
    original = (root / 'Packages/manifest.json').read_bytes()
    state = root / STATE
    state.mkdir()  # exclusive reservation: a second installer cannot share this environment
    receipt = {'schema': 1, 'status': 'preparing', 'unity_validated': False,
               'services_started': False, 'project': str(root), 'written_paths': []}
    receipt_path = state / 'install-result.json'
    write_json(receipt_path, receipt)
    try:
        lock = repo / INSTALL_LOCK
        receipt['requirements_sha256'] = hashlib.sha256(lock.read_bytes()).hexdigest()
        python = prepare_environment(state, lock)
        # Download may take time. Re-check ownership and project state before import-root writes.
        preflight(root, repo, owned_state=True)
        if (root / 'Packages/manifest.json').read_bytes() != original:
            raise ValueError('Project manifest changed during setup; installation stopped')
        (state / 'manifest.before.json').write_bytes(original)
        for source, relative in payload(repo):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            receipt['written_paths'].append(relative.as_posix())
            write_json(receipt_path, receipt)
            if source.is_dir():
                shutil.copytree(source, target, ignore=IGNORE)
            else:
                with target.open('xb') as stream:
                    stream.write(source.read_bytes())
        receipt['written_paths'].append('JobQueue')
        write_json(receipt_path, receipt)
        subprocess.run([str(python), '-s', str(root / 'Tools/UnityJobCoordinator/install.py')],
                       cwd=root, check=True)
        # Commit the package reference only after environment and coordinator setup succeed.
        if (root / 'Temp/UnityLockfile').exists():
            raise ValueError('Unity opened during installation. Close it and review the partial install.')
        if (root / 'Packages/manifest.json').read_bytes() != original:
            raise ValueError('Project manifest changed during setup; it was not overwritten')
        manifest['dependencies'][PACKAGE] = f'file:{PACKAGE}'
        pending = state / 'manifest.pending.json'
        write_json(pending, manifest)
        os.replace(pending, root / 'Packages/manifest.json')
        receipt['written_paths'].append('Packages/manifest.json')
        receipt['status'] = 'installed_pending_unity_validation'
        write_json(receipt_path, receipt)
        return receipt
    except Exception as error:
        receipt['status'] = 'failed'
        receipt['error'] = str(error)
        write_json(receipt_path, receipt)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True, help='Existing isolated Unity project, with its Editor closed')
    parser.add_argument('--check', action='store_true', help='Validate paths/conflicts only; no writes, downloads or Unity calls')
    args = parser.parse_args()
    try:
        if sys.platform != 'win32' or sys.version_info[:2] != (3, 13):
            raise ValueError('This preview requires Windows CPython 3.13 (hash-pinned Windows wheels)')
        if args.check:
            root, _ = preflight(args.project)
            result = {'status': 'preflight_passed', 'project': str(root), 'dependencies_checked': False}
        else:
            result = install(args.project)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Installation stopped: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
