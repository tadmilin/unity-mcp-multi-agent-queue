"""Real ledger + notification files, controlled authority. Not Unity acceptance."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from phase import PhaseWorker, PhaseBridge, coordinator
from import_recovery import ImportCleanupRecovery, validate_contract, oracle_code, eligible_tick, VERSION


class Authority:
    def __init__(self, case):
        self.case = case
        self.token = 'test-secret'
        self.client = SimpleNamespace(timeout=30)
        self.calls = []

    def close(self):
        pass

    def call(self, action, **p):
        self.calls.append(action)
        t = self.case
        if action == 'status':
            return {'success': True, 'data': copy.deepcopy(t.state)}
        if action == 'import':
            t.state['import']['state'] = 'complete'
            t.publish()
        elif action == 'write_files':
            for item in p['files']:
                data = (t.root/item['stagedPath']).read_bytes() if 'stagedPath' in item else base64.b64decode(item['base64'])
                (t.root/item['path']).write_bytes(data)
            t.state['receipt'] = {'id': p['operation'], 'state': 'complete', 'sha256': 'receipt', 'result': {'success': True}}
            t.state['import'] = {'state': 'staged', 'operation': p['operation'], 'requiresReload': False, 'originBootUtc': t.boot}
            t.publish()
            return {'success': True, 'data': {'result': {'success': True}}}
        elif action == 'execute':
            t.state['receipt'] = {'id': p['operation'], 'state': 'complete',
                'result': {'success': True, 'data': [] if p['command'] == 'read_console' else {'result': 'import-recovery-loaded'}}}
            t.publish()
            return {'success': True, 'data': {'result': copy.deepcopy(t.state['receipt']['result'])}}
        elif action == 'ack':
            t.state['receipt']['state'] = 'acknowledged'
            t.publish()
        elif action == 'release':
            if not t.state['editor']['idle']:
                raise AssertionError('unsafe release')
            t.state['holder'] = None
            t.state['import'] = None
            t.publish()
        return {'success': True, 'data': {}}


class ImportRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = coordinator.Store(self.root / 'JobQueue/phases')
        self.addCleanup(self.store.close)
        self.manifest = {'kind': 'files', 'play': False, 'depends_on': [], 'queue_timeout_seconds': 10,
            'files': [{'path': 'Assets/Probe.txt', 'base64': base64.b64encode(b'new').decode(), 'expectedSha256': 'missing'}],
            'import_recovery': [{'kind': 'text_asset', 'path': 'Assets/Probe.txt'}]}
        self.job = self.store.submit('test-import', self.manifest)
        self.store.update(self.job, state='quarantined', token='test-secret', ticket=7, result='{"reason":"original fault"}')
        (self.root / 'Assets').mkdir()
        (self.root / 'Assets/Probe.txt').write_bytes(b'new')
        self.boot = '2026-10-28T03:21:35.7592380Z'
        self.state = {'holder': {'ticket': 7, 'owner': 'coordinator-' + self.job, 'deadline': time.time()+60},
            'receipt': {'id': self.job+'-write', 'state': 'acknowledged', 'sha256': 'receipt', 'result': {'success': True}},
            'editor': {'pid': 22, 'scene': 'Assets/Original.unity', 'idle': True, 'dirty': False, 'playing': False, 'changing': False},
            'bootUtc': self.boot, 'import': {'state': 'staged', 'operation': self.job+'-write',
                'originBootUtc': self.boot, 'requiresReload': False}}
        self.connection = {'schema': 'native-import-notification-v1', 'workspace': str(self.root),
            'project_id': hashlib.sha1((self.root/'Assets').as_posix().encode()).hexdigest()[:16],
            'native_pid': 12, 'epoch': 'same', 'session_id': 'old'}
        self.store.event(self.job, 'import_recovery_origin', {'version': VERSION, 'digest': self.store.get(self.job)['digest'],
            'initial': copy.deepcopy(self.state), 'created_ns': time.time_ns(), 'connection': self.connection})
        self.store.event(self.job, 'import_recovery_staged', {'receipt_hash': 'receipt', 'requires_reload': False})
        self.store.event(self.job, 'intent', {'action': 'write_files', 'operation': self.job+'-write'})
        self.store.event(self.job, 'intent', {'action': 'ack', 'operation': self.job+'-write'})
        self.authority = Authority(self)
        self.worker = PhaseWorker(self.store, self.root, factory=lambda: self.authority)
        self.publish()
        mock = patch('import_recovery.live_unity_process', return_value=True)
        mock.start()
        self.addCleanup(mock.stop)

    def publish(self):
        state = copy.deepcopy(self.state)
        state['version'] = 1
        if state['holder']:
            state['holder'].update(secretHash=hashlib.sha256(b'test-secret').hexdigest(), operation=state['receipt'])
        for name, value in [('Library/EditorQueueGate/state.json', state),
                            ('Library/UnityWorkflowSignals/native-connected.json', self.connection)]:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))

    def run_recovery(self):
        return ImportCleanupRecovery(self.worker, self.job).run()

    def test_first_import_once_and_abandoned_not_success(self):
        self.assertTrue(self.run_recovery())
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.authority.calls.count('import'), 1)
        self.assertEqual(self.authority.calls.count('release'), 1)
        row = self.store.get(self.job)
        self.assertEqual(row['state'], 'abandoned')
        self.assertIsNone(row['token'])
        result = json.loads(row['result'])
        self.assertFalse(result['task_verified_success'])
        self.assertEqual(result['original_result']['reason'], 'original fault')

    def test_dispatch_ambiguous_staged_is_never_replayed(self):
        self.store.event(self.job, 'intent', {'action': 'import'})
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.authority.calls, ['status'])

    def test_import_complete_recovers_without_import(self):
        self.store.event(self.job, 'intent', {'action': 'import'})
        self.state['import']['state'] = 'complete'
        self.publish()
        self.assertTrue(self.run_recovery())
        self.assertNotIn('import', self.authority.calls)

    def test_missing_origin_does_not_touch_authority(self):
        self.store.db.execute("DELETE FROM events WHERE kind='import_recovery_origin'")
        self.store.db.commit()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.authority.calls, [])

    def test_mismatch_controls_no_mutations(self):
        for label, update in [
            ('owner', lambda: self.state['holder'].update(owner='other')),
            ('ticket', lambda: self.state['holder'].update(ticket=999)),
            ('expired', lambda: self.state['holder'].update(deadline=1)),
            ('dirty', lambda: self.state['editor'].update(dirty=True)),
            ('playing', lambda: self.state['editor'].update(playing=True)),
            ('pid', lambda: self.state['editor'].update(pid=999)),
            ('receipt', lambda: self.state['receipt'].update(sha256='other')),
            ('unknown', lambda: self.state['import'].update(state='unknown')),
            ('origin', lambda: self.state['import'].update(originBootUtc='2026-10-28T00:00:00Z'))]:
            with self.subTest(label=label):
                old = copy.deepcopy(self.state)
                self.authority.calls.clear()
                update()
                self.publish()
                self.assertFalse(self.run_recovery())
                self.assertTrue(all(a == 'status' for a in self.authority.calls))
                self.state = old

    def test_changed_file_no_mutation(self):
        (self.root/'Assets/Probe.txt').write_bytes(b'other')
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.authority.calls, ['status'])

    def test_duplicate_import_intent_refuses(self):
        for _ in range(2):
            self.store.event(self.job, 'intent', {'action': 'import'})
        self.assertFalse(self.run_recovery())
        self.assertNotIn('import', self.authority.calls)

    def test_restart_after_release_response_finalizes_without_rpc(self):
        event = self.store.event
        def lose(job, kind, detail):
            if kind == 'import_cleanup_recovered':
                raise OSError('crash before final ledger commit')
            return event(job, kind, detail)
        with patch.object(self.store, 'event', side_effect=lose):
            self.assertFalse(self.run_recovery())
        self.authority.calls.clear()
        self.assertTrue(self.run_recovery())
        self.assertEqual(self.authority.calls, [])

    def test_lost_release_reply_remains_quarantined_no_second_release(self):
        original = self.authority.call
        def call(action, **params):
            value = original(action, **params)
            if action == 'release':
                raise OSError('reply lost')
            return value
        with patch.object(self.authority, 'call', side_effect=call):
            self.assertFalse(self.run_recovery())
            self.assertFalse(self.run_recovery())
        self.assertEqual(self.authority.calls.count('release'), 1)
        self.assertEqual(self.store.get(self.job)['state'], 'quarantined')

    def test_independent_next_job_and_failed_dependency(self):
        independent = self.store.submit('independent', {**self.manifest, 'kind': 'check'})
        dependent = self.store.submit('dependent', {**self.manifest, 'depends_on': [self.job]})
        self.assertIsNone(self.store.next_job())
        self.assertTrue(self.run_recovery())
        self.assertEqual(self.store.next_job(), independent)
        self.store.update(independent, state='succeeded')
        self.assertIsNone(self.store.next_job())
        self.assertEqual(self.store.get(dependent)['state'], 'failed')

    def test_stable_quarantine_does_not_poll_authority(self):
        self.store.event(self.job, 'intent', {'action': 'import'})
        for _ in range(5):
            self.assertFalse(eligible_tick(self.worker, self.job))
        self.assertEqual(self.authority.calls, ['status'])

    def test_contract_and_oracle(self):
        validate_contract(self.manifest, self.manifest['import_recovery'])
        self.assertIn('LoadAssetAtPath<UnityEngine.TextAsset>', oracle_code(self.manifest))
        for field, value in [('play', True), ('kind', 'check'), ('edit_steps', [{'code':'bad'}])]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_contract({**self.manifest, field:value}, self.manifest['import_recovery'])

    def test_real_import_path_faults_and_recovers_once(self):
        # Remove fabricated checkpoints: the real import_files path must produce them.
        self.store.db.execute('DELETE FROM events WHERE job=?', (self.job,))
        self.store.db.commit()
        (self.root/'Assets/Probe.txt').unlink()
        self.state.update(persistenceVersion='fail-closed-v2', importScheduler='synchronous-refresh-v3', stagedFileVersion='sha256-v1')
        self.state['import'] = None
        self.state['receipt'] = None
        self.publish()
        (self.root/'Library/ImportRecoveryFaults.json').write_text(json.dumps({
            'version':1, 'submission_key':'test-import', 'digest':self.store.get(self.job)['digest'], 'point':'before_import_intent'}))
        self.worker.client = self.authority
        with self.assertRaisesRegex(RuntimeError, 'Owned import recovery fault'):
            self.worker.import_files(self.job, self.manifest)
        self.worker.client = None
        self.assertFalse(self.worker.safe)
        self.assertTrue(self.run_recovery())
        self.assertEqual(self.authority.calls.count('write_files'), 1)
        self.assertEqual(self.authority.calls.count('import'), 1)
        self.assertEqual(self.authority.calls.count('release'), 1)
        bridge = PhaseBridge(self.root, worker_check=lambda: True)
        outcome = bridge.outcome(bridge.snapshot(self.job, evidence=True))
        self.assertFalse(outcome['task_verified_success'])
        self.assertFalse(outcome['verified_success'])
        self.assertTrue(outcome['cleanup_recovered'])

    def test_boot_format_equivalence_and_reloaded_complete(self):
        self.store.event(self.job, 'intent', {'action': 'import'})
        self.state['import'].update(state='complete', requiresReload=True)
        self.state['bootUtc'] = '2026-10-28T03:40:00.0000000Z'
        self.connection['session_id'] = 'new'
        self.store.db.execute("UPDATE events SET detail=? WHERE kind='import_recovery_staged'", (json.dumps({'receipt_hash':'receipt','requires_reload':True}),))
        self.store.db.commit()
        self.publish()
        self.assertTrue(self.run_recovery())
        self.assertNotIn('import', self.authority.calls)

    def test_native_identity_mismatch_never_imports(self):
        self.connection['epoch'] = 'other-process'
        self.publish()
        self.assertFalse(self.run_recovery())
        self.assertEqual(self.authority.calls, ['status'])

    def test_failed_oracle_never_releases_or_replays(self):
        original = self.authority.call
        def wrong(action, **p):
            value = original(action, **p)
            if action == 'execute':
                self.state['receipt']['result'] = {'success':False, 'error':'loaded marker mismatch'}
                value['data']['result'] = self.state['receipt']['result']
                self.publish()
            return value
        with patch.object(self.authority, 'call', side_effect=wrong):
            self.assertFalse(self.run_recovery())
            self.assertFalse(self.run_recovery())
        self.assertNotIn('release',self.authority.calls)
        self.assertEqual(self.authority.calls.count('execute'),1)

    def test_prior_console_errors_preserved_without_false_success(self):
        original = self.authority.call
        def noisy(action, **p):
            value = original(action, **p)
            if action == 'execute' and p['command'] == 'read_console':
                self.state['receipt']['result']['data'] = ['Earlier other-job animation event error']
                value['data']['result'] = copy.deepcopy(self.state['receipt']['result'])
                self.publish()
            return value
        with patch.object(self.authority, 'call', side_effect=noisy):
            self.assertTrue(self.run_recovery())
        result = json.loads(self.store.get(self.job)['result'])
        self.assertEqual(len(result['console']['data']),1)
        self.assertFalse(result['task_verified_success'])


if __name__ == '__main__':
    unittest.main()
