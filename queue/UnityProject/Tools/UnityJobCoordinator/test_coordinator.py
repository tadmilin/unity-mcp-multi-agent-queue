"""Offline integration tests use real SQLite, files, OS locks and client processes."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from install import install
from store import Store, WorkerLock, digest, encode, snapshot_manifest, worker_running
from worker import Worker


CHECK = {'kind': 'check', 'files': [], 'depends_on': []}


class FakeGate:
    def __init__(self, root):
        self.root, self.token, self.calls = root, None, []
        self.holder, self.import_state, self.operation = None, None, None
        self.fail, self.bad_console, self.deny_claim = None, False, False
        self.on_claim = None
        self.ticket, self.owner, self.waiting = 0, None, False

    def close(self):
        pass

    def call(self, action, **params):
        self.calls.append(action)
        result = self.handle(action, params)
        if self.fail == action:
            raise OSError('Injected lost reply after server action')
        return result

    def handle(self, action, params):
        data = None
        if action == 'status':
            if params.get('token') is not None and self.holder is None:
                return {'success': False, 'error': 'EDITOR_QUEUE_TOKEN_INVALID'}
            data = {'holder': self.holder, 'waiting': 0, 'editor': {'idle': True},
                    'import': self.import_state, 'autoRefreshHeld': bool(self.holder),
                    'persistenceVersion': 'fail-closed-v2', 'importScheduler': 'synchronous-refresh-v3',
                    'stagedFileVersion': 'sha256-v1', 'receipt': None}
        elif action == 'request':
            self.ticket += 1
            self.owner, self.waiting = params['owner'], True
            data = {'token': 'PRIVATE_BEARER', 'ticket': self.ticket}
        elif action == 'claim':
            if self.on_claim:
                self.on_claim()
            if self.deny_claim:
                return {'success': False, 'message': 'EDITOR_QUEUE_HELD'}
            self.holder = {'owner': self.owner, 'ticket': self.ticket, 'blocked': None}
            self.waiting = False
        elif action == 'cancel':
            if not self.waiting:
                return {'success': False, 'error': 'EDITOR_QUEUE_WAITING_TOKEN_REQUIRED'}
            self.waiting = False
        elif action == 'write_files':
            self.operation = params['operation']
            for item in params['files']:
                path = self.root / item['path']
                path.parent.mkdir(parents=True, exist_ok=True)
                if item.get('delete'):
                    path.unlink(missing_ok=True)
                else:
                    content = (self.root / item['stagedPath']).read_bytes() if 'stagedPath' in item else base64.b64decode(item['base64'])
                    path.write_bytes(content)
            self.import_state = {'state': 'staged', 'operation': self.operation}
            data = {'result': {'success': True}}
        elif action == 'import':
            self.import_state['state'] = 'complete'
        elif action == 'execute':
            data = {'result': {'success': not self.bad_console, 'data': []}}
        elif action == 'release':
            self.holder, self.import_state = None, None
        return {'success': True, 'data': data}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'Assets').mkdir()
        self.home = self.root / 'JobQueue'
        self.store = Store(self.home)
        self.gate = FakeGate(self.root)
        # Real FULL SQLite commits must not race a 30 ms test deadline on Windows.
        # Timeout-specific cases set their own budget; ordinary cases test protocol.
        self.worker = Worker(self.store, self.root, factory=lambda: self.gate, timeout=2, interval=.001)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, manifest=None, key='test'):
        return self.store.submit(key, manifest or CHECK)

    def process(self, manifest=None):
        job = self.submit(manifest)
        self.assertEqual(job, self.store.next_job())
        self.worker.process(job)
        return self.store.get(job)

    def files(self):
        return {'kind': 'files', 'depends_on': [], 'files': [
            {'path': 'Assets/Test.txt', 'expectedSha256': 'missing',
             'base64': base64.b64encode(b'payload').decode()}]}

    def manifest(self, data):
        path = self.root / 'manifest.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def test_idempotent_submission(self):
        self.assertEqual(self.submit(), self.submit())
        with self.assertRaises(ValueError):
            self.submit(self.files())

    def test_source_snapshot_survives_source_edit(self):
        source = self.root / 'source.txt'
        source.write_bytes(b'original')
        path = self.manifest({'files': [{'path': 'Assets/New.txt', 'source': 'source.txt',
                                        'expectedSha256': 'missing'}]})
        manifest = snapshot_manifest(self.root, path)
        source.write_bytes(b'changed')
        row = self.process(manifest)
        self.assertEqual('succeeded', row['state'])
        self.assertEqual(b'original', (self.root / 'Assets/New.txt').read_bytes())

    def test_invalid_paths_and_fields(self):
        for relative in ('Packages/X.cs', 'Assets/../X.cs', 'Assets\\X.cs', 'Assets/X:stream',
                         'Assets//X.cs', '/Assets/X.cs', 'Assets/X. '):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                snapshot_manifest(self.root, self.manifest({'files': [
                    {'path': relative, 'delete': True, 'expectedSha256': 'missing'}]}))
        with self.assertRaises(ValueError):
            snapshot_manifest(self.root, self.manifest({'kind': 'check', 'execute_code': 'anything'}))

    def test_duplicate_windows_path_rejected(self):
        with self.assertRaises(ValueError):
            snapshot_manifest(self.root, self.manifest({'files': [
                {'path': p, 'delete': True, 'expectedSha256': 'missing'}
                for p in ('Assets/X.cs', 'Assets/x.cs')]}))

    def test_explicit_expected_hash_required(self):
        with self.assertRaises(ValueError):
            snapshot_manifest(self.root, self.manifest({'files': [{'path': 'Assets/X.cs', 'delete': True}]}))

    def test_empty_files_rejected(self):
        with self.assertRaises(ValueError):
            snapshot_manifest(self.root, self.manifest({'kind': 'files'}))

    def test_payload_limit(self):
        source = self.root / 'large.bin'
        with source.open('wb') as stream:
            stream.truncate(10 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            snapshot_manifest(self.root, self.manifest({'files': [{'path': 'Assets/X.bin',
                'source': 'large.bin', 'expectedSha256': 'missing'}]}))

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(ValueError):
            self.submit(CHECK | {'depends_on': ['missing']})

    def test_dependencies_and_single_claim(self):
        first = self.submit()
        second = self.submit(CHECK | {'depends_on': [first]}, key='second')
        self.assertEqual(first, self.store.next_job())
        other = Store(self.home)
        try:
            self.assertIsNone(other.next_job())
        finally:
            other.close()
        self.worker.process(first)
        self.assertEqual(second, self.store.next_job())

    def test_failed_dependency_does_not_execute(self):
        first = self.submit()
        second = self.submit(CHECK | {'depends_on': [first]}, key='second')
        self.store.update(first, state='failed')
        self.assertIsNone(self.store.next_job())
        self.assertEqual('failed', self.store.get(second)['state'])

    def test_check_releases_and_clears_token(self):
        row = self.process()
        self.assertEqual('succeeded', row['state'])
        self.assertIsNone(row['token'])
        self.assertEqual(1, self.gate.calls.count('release'))
        self.assertNotIn('import', self.gate.calls)
        self.assertNotIn('PRIVATE_BEARER', encode(self.store.public(row['id'])))

    def test_files_import_once_and_verify(self):
        row = self.process(self.files())
        self.assertEqual('succeeded', row['state'])
        self.assertEqual(1, self.gate.calls.count('write_files'))
        self.assertEqual(1, self.gate.calls.count('import'))
        self.assertEqual(b'payload', (self.root / 'Assets/Test.txt').read_bytes())
        public = self.store.public(row['id'])
        self.assertEqual('files', public['kind'])
        self.assertGreaterEqual(public['import_seconds'], 0)

    def test_delete_job(self):
        target = self.root / 'Assets/Test.txt'
        target.write_bytes(b'original')
        manifest = self.files()
        manifest['files'] = [{'path': 'Assets/Test.txt', 'delete': True,
                              'expectedSha256': digest(b'original')}]
        self.assertEqual('succeeded', self.process(manifest)['state'])
        self.assertFalse(target.exists())

    def test_stale_hash_rejected_before_gate_request(self):
        (self.root / 'Assets/Test.txt').write_bytes(b'existing')
        self.assertEqual('failed', self.process(self.files())['state'])
        self.assertEqual([], self.gate.calls)

    def test_corrupt_payload_rejected(self):
        job = self.submit()
        self.store.next_job()
        with self.store.db:
            self.store.db.execute('UPDATE jobs SET digest=? WHERE id=?', ('corrupt', job))
        self.worker.process(job)
        self.assertEqual('failed', self.store.get(job)['state'])
        self.assertEqual([], self.gate.calls)

    def test_lost_write_reply_quarantines_without_replay(self):
        self.gate.fail = 'write_files'
        row = self.process(self.files())
        self.assertEqual('quarantined', row['state'])
        self.assertEqual('PRIVATE_BEARER', row['token'])
        self.assertNotIn('PRIVATE_BEARER', encode(self.store.public(row['id'])))
        self.assertEqual(1, self.gate.calls.count('write_files'))
        self.assertNotIn('import', self.gate.calls)
        self.assertNotIn('release', self.gate.calls)

    def test_lost_import_reply_quarantines_without_replay(self):
        self.gate.fail = 'import'
        row = self.process(self.files())
        self.assertEqual('quarantined', row['state'])
        self.assertEqual(1, self.gate.calls.count('import'))
        self.assertNotIn('release', self.gate.calls)

    def test_non_success_status_during_reload_reconnects_without_import_replay(self):
        original = self.gate.handle
        failures = [0]
        def handle(action, params):
            if action == 'status' and self.gate.import_state and failures[0] == 0:
                failures[0] += 1
                return {}  # Existing tool_value maps MCP transport error responses to {}.
            return original(action, params)
        self.gate.handle = handle
        row = self.process(self.files())
        self.assertEqual('succeeded', row['state'])
        self.assertEqual(1, failures[0])
        self.assertEqual(1, self.gate.calls.count('write_files'))
        self.assertEqual(1, self.gate.calls.count('import'))

    def test_compile_failure_stays_quarantined(self):
        original = self.gate.handle
        def handle(action, params):
            result = original(action, params)
            if action == 'import':
                self.gate.import_state['state'] = 'compile_failed'
            return result
        self.gate.handle = handle
        self.assertEqual('quarantined', self.process(self.files())['state'])
        self.assertNotIn('release', self.gate.calls)

    def test_final_read_reconnect_does_not_repeat_console_or_import(self):
        original = self.gate.handle
        seen_console, failures = [], []
        def handle(action, params):
            if action == 'execute':
                seen_console.append(True)
            if action == 'status' and seen_console and not failures:
                failures.append(True)
                return {'success':False, 'error':'Temporary observation failure'}
            return original(action, params)
        self.gate.handle = handle
        self.assertEqual('succeeded', self.process(self.files())['state'])
        self.assertEqual(1, len(failures))
        for action in ('execute', 'write_files', 'import', 'release'):
            self.assertEqual(1, self.gate.calls.count(action))

    def test_lost_successful_claim_observed_without_repeating_claim(self):
        self.gate.fail = 'claim'
        row = self.process()
        self.assertEqual('succeeded', row['state'])
        self.assertEqual(1, self.gate.calls.count('claim'))
        self.assertEqual(1, self.gate.calls.count('request'))

    def test_claim_deadline_cancels_unclaimed_ticket_only(self):
        self.worker.timeout = 0
        row = self.process()
        self.assertEqual('cancelled', row['state'])
        self.assertNotIn('claim', self.gate.calls)
        self.assertEqual(1, self.gate.calls.count('cancel'))
        self.assertNotIn('release', self.gate.calls)

    def test_lost_claim_with_existing_receipt_is_not_recovered(self):
        self.gate.fail = 'claim'
        original = self.gate.handle
        def handle(action, params):
            response = original(action, params)
            if action == 'status' and self.gate.holder:
                response['data']['receipt'] = {'id': 'unexpected', 'state': 'complete'}
            return response
        self.gate.handle = handle
        self.assertEqual('quarantined', self.process()['state'])
        self.assertNotIn('execute', self.gate.calls)
        self.assertNotIn('release', self.gate.calls)

    def test_claim_timeout_before_execution_retires_waiter_before_new_ticket(self):
        original = self.gate.handle
        failures = [0]
        def handle(action, params):
            if action == 'claim' and failures[0] == 0:
                failures[0] += 1
                return {'success': False, 'error': 'TimeoutError'}
            return original(action, params)
        self.gate.handle = handle
        row = self.process(self.files())
        self.assertEqual('succeeded', row['state'])
        self.assertEqual(2, self.gate.calls.count('request'))
        self.assertEqual(1, self.gate.calls.count('cancel'))
        self.assertEqual(1, self.gate.calls.count('write_files'))
        self.assertEqual(1, self.gate.calls.count('import'))

    def test_lost_cancel_never_requests_replacement_ticket(self):
        original = self.gate.handle
        def handle(action, params):
            if action == 'claim':
                return {'success': False, 'error': 'TimeoutError'}
            return original(action, params)
        self.gate.handle = handle
        self.gate.fail = 'cancel'
        self.assertEqual('quarantined', self.process()['state'])
        self.assertEqual(1, self.gate.calls.count('request'))
        self.assertNotIn('write_files', self.gate.calls)

    def test_foreign_busy_read_timeout_does_not_attempt_claim(self):
        original = self.gate.handle
        reads = [0]
        def handle(action, params):
            if action == 'status' and self.gate.waiting:
                reads[0] += 1
                if reads[0] == 1:
                    return {'success': False, 'error': 'TimeoutError'}
                if reads[0] == 2:
                    result = original(action, params)
                    result['data']['holder'] = {'owner': 'foreign'}
                    result['data']['editor']['idle'] = False
                    return result
            return original(action, params)
        self.gate.handle = handle
        self.assertEqual('succeeded', self.process()['state'])
        self.assertEqual(1, self.gate.calls.count('claim'))

    def test_large_payload_uses_small_staged_descriptor(self):
        manifest = self.files()
        manifest['files'][0]['base64'] = base64.b64encode(b'x' * (4 * 1024 * 1024)).decode()
        job = self.submit(manifest)
        state = self.gate.handle('status', {})['data']
        wire = self.worker.wire_files(job, manifest, state)
        self.assertLess(len(encode(wire)), 1024)
        self.assertNotIn('base64', wire[0])
        self.assertEqual(digest(b'x' * (4 * 1024 * 1024)), wire[0]['stagedSha256'])
        self.store.next_job()
        self.worker.process(job)
        self.assertEqual('succeeded', self.store.get(job)['state'])
        self.assertEqual(4 * 1024 * 1024, (self.root / 'Assets/Test.txt').stat().st_size)

    def test_large_payload_rejects_old_gate_before_request(self):
        manifest = self.files()
        manifest['files'][0]['base64'] = base64.b64encode(b'x' * (300 * 1024)).decode()
        original = self.gate.handle
        def handle(action, params):
            result = original(action, params)
            if action == 'status':
                result['data'].pop('stagedFileVersion')
            return result
        self.gate.handle = handle
        self.assertEqual('failed', self.process(manifest)['state'])
        self.assertNotIn('request', self.gate.calls)

    def test_changed_staged_payload_is_rejected_before_gate_request(self):
        manifest = self.files()
        manifest['files'][0]['base64'] = base64.b64encode(b'x' * (300 * 1024)).decode()
        job = self.submit(manifest)
        wire = self.worker.wire_files(job, manifest, {'stagedFileVersion': 'sha256-v1'})
        (self.root / wire[0]['stagedPath']).write_bytes(b'changed')
        self.store.next_job()
        self.worker.process(job)
        self.assertEqual('failed', self.store.get(job)['state'])
        self.assertNotIn('request', self.gate.calls)

    def test_gate_client_pins_each_call_to_explicit_instance(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'UnityEditorGate'))
        import gate_client
        with patch.object(gate_client, 'Client') as client, patch.object(gate_client, 'tool_value', side_effect=lambda x: x):
            gate = gate_client.GateClient(instance='Project@exacthash')
            gate.call('status')
            arguments = client.return_value.rpc.call_args.args[1]['arguments']
            self.assertEqual('Project@exacthash', arguments['unity_instance'])
            self.assertEqual('status', arguments['parameters']['action'])
            gate.close()

    def test_lost_request_stops_other_jobs(self):
        self.gate.fail = 'request'
        row = self.process()
        self.submit(key='later')
        self.assertEqual('quarantined', row['state'])
        self.assertIsNone(row['token'])
        self.assertIsNone(self.store.next_job())

    def test_inner_handler_failure_not_success(self):
        self.gate.bad_console = True
        row = self.process()
        self.assertEqual('failed', row['state'])
        self.assertIsNone(row['token'])

    def test_interrupted_job_is_not_replayed(self):
        job = self.submit()
        self.store.next_job()
        self.assertEqual(2, self.worker.run(once=True))
        self.assertEqual('quarantined', self.store.get(job)['state'])
        self.assertEqual([], self.gate.calls)

    def test_stop_waiter_cancels_only_own_ticket(self):
        self.gate.deny_claim = True
        self.gate.on_claim = lambda: self.store.control('stop', '1')
        row = self.process()
        self.assertEqual('cancelled', row['state'])
        self.assertIn('cancel', self.gate.calls)
        self.assertNotIn('release', self.gate.calls)

    def test_stop_after_claim_drains_job(self):
        self.gate.on_claim = lambda: self.store.control('stop', '1')
        self.assertEqual('succeeded', self.process()['state'])
        self.assertIn('release', self.gate.calls)

    def test_worker_lock_excludes_second_worker(self):
        with WorkerLock(self.home):
            self.assertTrue(worker_running(self.home))
            with self.assertRaises(RuntimeError):
                self.worker.run(once=True)
        self.assertFalse(worker_running(self.home))

    def test_four_real_producer_processes(self):
        clients = [subprocess.Popen([sys.executable, __file__, '--producer', str(self.home), str(n)],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE) for n in range(4)]
        for client in clients:
            _, error = client.communicate(timeout=30)
            self.assertEqual(0, client.returncode, error.decode())
        self.assertEqual(21, self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0])
        self.assertEqual('ok', self.store.db.execute('PRAGMA integrity_check').fetchone()[0])

    def test_reinstall_keeps_jobs_and_backs_up_database(self):
        for folder in ('UnityJobCoordinator', 'UnityEditorGate', 'UnityReloadDiagnostics'):
            (self.root / 'Tools' / folder).mkdir(parents=True)
        for name in ('cli.py', 'store.py', 'worker.py', 'install.py'):
            shutil.copy2(Path(__file__).with_name(name), self.root / 'Tools/UnityJobCoordinator' / name)
        for relative in ('UnityEditorGate/gate_client.py', 'UnityReloadDiagnostics/mcp_cli.py'):
            (self.root / 'Tools' / relative).write_text('# test fixture', encoding='utf-8')
        job = self.submit()
        install(self.root)
        install(self.root)
        self.assertEqual('queued', self.store.get(job)['state'])
        self.assertEqual(2, len(list((self.root / 'Backups/UnityJobCoordinatorInstall').glob('*/queue.sqlite3'))))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--producer':
        store = Store(Path(sys.argv[2]))
        try:
            store.submit('shared-key', CHECK)
            for number in range(5):
                store.submit(sys.argv[3] + '-' + str(number), CHECK)
        finally:
            store.close()
    else:
        unittest.main(verbosity=2)
