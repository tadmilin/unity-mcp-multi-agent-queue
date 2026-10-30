"""Safety regressions for notification wait. Not Unity acceptance."""
import contextlib
import copy
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import sys

from import_wait import DirectoryChanges, ImportWait, ImportWaitError, boot_id
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'UnityNativeObserver'))
from import_notifications import write_snapshot


class ImportWaitTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.gate = self.root / 'Library/EditorQueueGate/state.json'
        self.native = self.root / 'Library/UnityWorkflowSignals/native-connected.json'
        for path in (self.gate, self.native):
            path.parent.mkdir(parents=True)
        self.connection = {'schema': 'native-import-notification-v1', 'workspace': str(self.root),
            'project_id': sha1((self.root / 'Assets').as_posix().encode()).hexdigest()[:16],
            'native_pid': 12, 'epoch': 'same-process', 'session_id': 'old'}
        self.state = {'version': 1, 'holder': {'ticket': 7, 'owner': 'coordinator-job',
            'secretHash': sha256(b'own-secret').hexdigest(), 'deadline': time.time() + 30,
            'operation': {'id': 'job-write', 'state': 'acknowledged', 'sha256': 'own-write-digest',
                          'result': {'success': True}}},
            'import': {'operation': 'job-write', 'originBootUtc': '2026-10-28T03:21:35.7592380Z', 'requiresReload': True, 'state': 'staged'}}
        self.initial = {'bootUtc': '2026-10-28T03:21:35.759238Z', 'editor': {'pid': 22}}
        self.calls, self.events = [], []
        self.live = None
        def call(action, **parameters):
            self.calls.append(action)
            return self.live
        self.worker = SimpleNamespace(root=self.root, timeout=1,
            client=SimpleNamespace(token='own-secret', client=SimpleNamespace(timeout=30), call=call),
            store=SimpleNamespace(get=lambda job: {'ticket': 7},
                                  event=lambda job, kind, data: self.events.append((kind, data))))
        self.write(self.native, self.connection)
        self.write(self.gate, self.state)

    def write(self, path, value):
        write_snapshot(path, value)

    def observer(self):
        result = ImportWait(self.worker, 'job', 'job-write', self.initial)
        self.addCleanup(result.close)
        result.staged()
        return result

    def complete(self, reload=True):
        self.state['import']['state'] = 'complete'
        self.connection['session_id'] = 'new' if reload else 'old'
        self.live = {'success': True, 'data': {
            'holder': copy.deepcopy(self.state['holder']),
            'receipt': copy.deepcopy(self.state['holder']['operation']),
            'import': copy.deepcopy(self.state['import']),
            'bootUtc': '2026-10-28T03:40:12.5Z' if reload else '2026-10-28T03:21:35.759238Z', 'editor': {'pid': 22, 'idle': True}}}
        self.write(self.gate, self.state)
        self.write(self.native, self.connection)

    def test_real_windows_events_wait_without_rpc_then_confirm_once(self):
        observation = self.observer()
        self.state['import']['state'] = 'settling'
        self.write(self.gate, self.state)
        def notify():
            if self.calls:
                self.live = None
                return
            self.complete()
        timer = threading.Timer(.05, notify)
        timer.start()
        try:
            self.assertTrue(observation.wait()['editor']['idle'])
        finally:
            timer.join()
        self.assertEqual(self.calls, ['status'])
        self.assertGreater(observation.wakes, 0)
        self.assertEqual(self.worker.client.client.timeout, 30)

    def test_preexisting_notification_not_lost(self):
        observation = self.observer()
        self.complete()
        observation.wait()
        self.assertEqual(self.calls, ['status'])

    def test_no_reload_waits_for_gate_completion(self):
        self.state['import']['requiresReload'] = False
        self.write(self.gate, self.state)
        observation = self.observer()
        self.complete(reload=False)
        observation.wait()
        self.assertEqual(self.calls, ['status'])

    def test_timeout_has_no_rpc_or_reconnect(self):
        observation = self.observer()
        observation.deadline = time.monotonic() + .03
        self.state['import']['state'] = 'reloading'
        self.write(self.gate, self.state)
        with self.assertRaisesRegex(ImportWaitError, 'deadline'):
            observation.wait()
        self.assertEqual(self.calls, [])

    def test_disk_complete_with_preserved_session_confirms_live_reload(self):
        observation = self.observer()
        self.complete()
        self.connection['session_id'] = 'old'
        self.write(self.native, self.connection)
        self.assertTrue(observation.wait()['editor']['idle'])
        self.assertEqual(self.calls, ['status'])

    def test_changed_authority_evidence_rejected_before_rpc(self):
        cases = [('holder', 'ticket', 8), ('holder', 'owner', 'foreign'),
                 ('holder', 'secretHash', 'foreign'), ('holder', 'blocked', 'quarantine'),
                 ('holder', 'deadline', time.time() - 1), ('import', 'operation', 'other-write'),
                 ('import', 'originBootUtc', 'other-boot'), ('import', 'requiresReload', False),
                 ('import', 'state', 'compile_failed')]
        original = copy.deepcopy(self.state)
        for section, key, value in cases:
            with self.subTest(section=section, key=key):
                self.state = copy.deepcopy(original)
                self.write(self.gate, self.state)
                with self.observer() as observation:
                    self.state['import']['state'] = 'reloading'
                    self.state[section][key] = value
                    self.write(self.gate, self.state)
                    with self.assertRaises(ImportWaitError):
                        observation.wait()
        self.assertEqual(self.calls, [])

    def test_changed_receipt_rejected(self):
        observation = self.observer()
        self.state['holder']['operation']['sha256'] = 'foreign'
        self.write(self.gate, self.state)
        with self.assertRaises(ImportWaitError):
            observation.wait()
        self.assertEqual(self.calls, [])

    def test_restarted_native_rejected(self):
        observation = self.observer()
        self.complete()
        self.connection['epoch'] = 'restarted'
        self.write(self.native, self.connection)
        with self.assertRaises(ImportWaitError):
            observation.wait()
        self.assertEqual(self.calls, [])

    def test_unavailable_confirmation_not_retried(self):
        observation = self.observer()
        self.complete()
        self.live = {'success': False}
        with self.assertRaisesRegex(ImportWaitError, 'unavailable'):
            observation.wait()
        self.assertEqual(self.calls, ['status'])

    def test_live_confirmation_rejects_wrong_boot_receipt_pid_idle(self):
        for case in ('boot', 'receipt', 'pid', 'idle', 'ticket'):
            with self.subTest(case=case):
                self.state['import']['state'] = 'staged'
                self.connection['session_id'] = 'old'
                self.write(self.gate, self.state)
                self.write(self.native, self.connection)
                with self.observer() as observation:
                    self.complete()
                    data = self.live['data']
                    if case == 'boot': data['bootUtc'] = '2026-10-28T03:21:35.7592380Z'
                    if case == 'receipt': data['receipt']['sha256'] = 'foreign'
                    if case == 'pid': data['editor']['pid'] = 23
                    if case == 'idle': data['editor']['idle'] = False
                    if case == 'ticket': data['holder']['ticket'] = 8
                    with self.assertRaisesRegex(ImportWaitError, 'mismatch'):
                        observation.wait()
        self.assertEqual(self.calls, ['status'] * 5)

    def test_boot_id_canonical_forms(self):
        # Gate disk "o" form vs MCP form with the trailing zero dropped (28 Oct ticket736).
        self.assertEqual(boot_id('2026-10-28T03:21:35.7592380Z'), boot_id('2026-10-28T03:21:35.759238Z'))
        self.assertNotEqual(boot_id('2026-10-28T03:21:35.7592381Z'), boot_id('2026-10-28T03:21:35.759238Z'))
        self.assertEqual('2026-10-28T03:21:35.0000000Z', boot_id('2026-10-28T03:21:35Z'))
        for bad in (None, '', 'old-boot', '2026-10-28T03:21:35.12345678Z', '2026-10-28 03:21:35Z',
                    '2026-10-28T03:21:35+00:00'):
            with self.subTest(bad=bad), self.assertRaises(ImportWaitError):
                boot_id(bad)

    def test_missing_producer_refuses_before_write(self):
        self.native.unlink()
        with self.assertRaises(OSError):
            self.observer()
        self.assertEqual(self.calls, [])

    def test_shared_reads_and_handle_cleanup(self):
        with self.observer() as observation:
            for _ in range(10):
                observation.snapshot()
                self.write(self.gate, self.state)
        self.assertEqual(observation.changes.handles, [])


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ImportWaitTests))
    args.output.write_text(json.dumps({'passed': result.wasSuccessful(), 'tests': result.testsRun,
        'scope': 'Safety regressions with Windows notifications; not Unity acceptance',
        'failures': [str(x) for x in result.failures], 'errors': [str(x) for x in result.errors]}, indent=2))
    raise SystemExit(not result.wasSuccessful())
