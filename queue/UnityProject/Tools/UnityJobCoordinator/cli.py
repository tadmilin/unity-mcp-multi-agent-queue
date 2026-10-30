"""Workspace-local queue commands. All public output excludes bearer tokens."""
import argparse
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time

from store import Store, WorkerLock, digest, encode, snapshot_manifest, worker_running
from worker import Worker, gate_factory

ROOT = Path(__file__).resolve().parents[2]
HOME = ROOT / 'JobQueue'


def metrics(store):
    rows = [store.public(r[0], events=False) for r in store.db.execute('SELECT id FROM jobs')]
    counts = {s: sum(r['state'] == s for r in rows) for s in sorted({r['state'] for r in rows})}
    summaries = {}
    for kind in sorted({r['kind'] for r in rows}):
        summaries[kind] = {}
        for key in ('queue_seconds', 'gate_wait_seconds', 'import_seconds', 'total_seconds'):
            values = sorted(r[key] for r in rows if r[key] is not None and
                            r['kind'] == kind and r['state'] == 'succeeded')
            summaries[kind][key] = {'samples': len(values), 'median': statistics.median(values) if values else None,
                'p95': values[max(0, math.ceil(len(values) * .95) - 1)] if values else None}
    return {'counts': counts, 'successful_timings_by_kind': summaries,
        'interpretation': 'Observed timings, not a controlled speed comparison; check jobs do not compile.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('start', 'stop', 'status', 'metrics', 'doctor'):
        commands.add_parser(name)
    run = commands.add_parser('run', help='Foreground worker')
    run.add_argument('--once', action='store_true')
    submit = commands.add_parser('submit')
    submit.add_argument('manifest', type=Path)
    submit.add_argument('--key', required=True, help='Stable key; identical resubmission returns the same ID')
    result = commands.add_parser('result')
    result.add_argument('job')
    resolve = commands.add_parser('resolve', help='Close a quarantine as abandoned after manual gate cleanup')
    resolve.add_argument('job')
    resolve.add_argument('--evidence', required=True, type=Path)
    args = parser.parse_args(argv)
    if not (HOME / 'installed.json').is_file():
        parser.error('Run python Tools/UnityJobCoordinator/install.py first')
    store = Store(HOME)
    try:
        if args.command == 'submit':
            job = store.submit(args.key, snapshot_manifest(ROOT, args.manifest))
            output = {'job': job, 'state': store.get(job)['state']}
        elif args.command == 'result':
            output = store.public(args.job)
        elif args.command == 'metrics':
            output = metrics(store)
        elif args.command == 'status':
            output = {'worker_running': worker_running(HOME), 'stop_requested': store.control('stop') == '1',
                'jobs': [store.public(r[0], events=False) for r in store.db.execute(
                    'SELECT id FROM jobs ORDER BY created DESC LIMIT 20')]}
        elif args.command == 'doctor':
            client = gate_factory()
            try:
                output = {'database': store.db.execute('PRAGMA integrity_check').fetchone()[0],
                          'gate': client.call('status', token=None)}
            finally:
                client.close()
        elif args.command == 'stop':
            store.control('stop', '1')
            output = {'stop_requested': True, 'behavior': 'Drain claimed job; cancel own unclaimed ticket; never kill Unity'}
        elif args.command == 'start':
            if worker_running(HOME):
                output = {'worker_running': True, 'already_running': True}
            else:
                if store.db.execute("SELECT 1 FROM jobs WHERE state='quarantined'").fetchone():
                    raise RuntimeError('Quarantine requires evidence and gate cleanup before restart')
                with (HOME / 'worker.log').open('ab') as stream:
                    process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), 'run'],
                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and process.poll() is None and not worker_running(HOME):
                    time.sleep(.1)
                if not worker_running(HOME):
                    raise RuntimeError('Worker did not start; inspect JobQueue/worker.log and status')
                output = {'worker_running': True, 'pid': process.pid, 'log': str(HOME / 'worker.log')}
        elif args.command == 'run':
            return Worker(store, ROOT).run(once=args.once)
        elif args.command == 'resolve':
            evidence = args.evidence.resolve()
            if not evidence.is_relative_to(ROOT) or not evidence.is_file() or evidence.stat().st_size == 0:
                raise ValueError('Provide a nonempty evidence file inside this workspace')
            with WorkerLock(HOME):
                if store.get(args.job)['state'] != 'quarantined':
                    raise ValueError('Job is not quarantined')
                client = gate_factory()
                try:
                    response = client.call('status', token=None)
                finally:
                    client.close()
                state = response.get('data') or {}
                if (response.get('success') is not True or state.get('holder') is not None or
                        state.get('waiting') != 0 or state.get('import') is not None or
                        state.get('autoRefreshHeld') is not False or not state.get('editor', {}).get('idle')):
                    raise RuntimeError('Gate is not fully released and clean; no database changes')
                detail = {'evidence': str(evidence.relative_to(ROOT)), 'sha256': digest(evidence.read_bytes()),
                          'gate': state, 'note': 'Abandoned after operator cleanup; never counts as success'}
                store.event(args.job, 'operator_resolution', detail)
                store.update(args.job, state='abandoned', phase='done', token=None,
                             finished=time.time(), result=encode(detail))
                output = store.public(args.job)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=True), file=sys.stderr)
        raise SystemExit(1)
