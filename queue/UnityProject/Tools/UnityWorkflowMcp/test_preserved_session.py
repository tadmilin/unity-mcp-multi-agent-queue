"""Candidate-only regressions using actual Windows notifications, not Unity acceptance."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys
import time
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Tools/UnityNativeObserver'))
spec = importlib.util.spec_from_file_location('original_tests', ROOT / 'Tools/UnityWorkflowMcp/test_import_wait.py')
original = importlib.util.module_from_spec(spec)
spec.loader.exec_module(original)
from import_wait import ImportWaitError


class PreservedSessionTests(original.ImportWaitTests):
    def preserved_complete(self):
        self.complete()
        self.connection['session_id'] = 'old'
        self.write(self.native, self.connection)

    def test_disk_complete_without_reconnect_does_not_accept_reload(self):
        # Replace the old timeout expectation: a hint permits one live check.
        observation = self.observer()
        self.preserved_complete()
        observation.deadline = time.monotonic() + .1
        self.assertTrue(observation.wait()['editor']['idle'])
        self.assertEqual(self.calls, ['status'])
        self.assertEqual(self.worker.client.client.timeout, 30)
        notified = [data for kind, data in self.events if kind == 'import_wait_notified']
        self.assertFalse(notified[0]['session_changed'])

    def test_preserved_session_completion_event_wakes_existing_wait(self):
        observation = self.observer()
        self.state['import']['state'] = 'reloading'
        self.write(self.gate, self.state)
        timer = threading.Timer(.05, self.preserved_complete)
        timer.start()
        try:
            observation.wait()
        finally:
            timer.join()
        self.assertGreater(observation.wakes, 0)
        self.assertEqual(self.calls, ['status'])
        self.assertEqual(self.worker.client.client.timeout, 30)

    def test_preserved_session_rejects_false_completion(self):
        baseline = copy.deepcopy(self.state)
        for case in ('boot', 'receipt', 'pid', 'idle', 'ticket', 'owner', 'operation', 'state', 'origin', 'lease'):
            with self.subTest(case=case):
                self.state = copy.deepcopy(baseline)
                self.write(self.gate, self.state)
                with self.observer() as observation:
                    self.preserved_complete()
                    data = self.live['data']
                    if case == 'boot': data['bootUtc'] = self.initial['bootUtc']
                    if case == 'receipt': data['receipt']['sha256'] = 'foreign'
                    if case == 'pid': data['editor']['pid'] = 23
                    if case == 'idle': data['editor']['idle'] = False
                    if case == 'ticket': data['holder']['ticket'] = 8
                    if case == 'owner': data['holder']['owner'] = 'foreign'
                    if case == 'operation': data['import']['operation'] = 'foreign'
                    if case == 'state': data['import']['state'] = 'reloading'
                    if case == 'origin': data['import']['originBootUtc'] = '2026-10-29T00:00:00Z'
                    if case == 'lease': data['holder']['deadline'] = time.time() - 1
                    observation.deadline = time.monotonic() + .1
                    with self.assertRaisesRegex(ImportWaitError, 'Live import confirmation mismatch'):
                        observation.wait()
        self.assertEqual(self.calls, ['status'] * 10)

    def test_preserved_session_unavailable_confirm_never_retries(self):
        observation = self.observer()
        self.preserved_complete()
        self.live = {'success': False}
        observation.deadline = time.monotonic() + .1
        with self.assertRaisesRegex(ImportWaitError, 'confirmation unavailable'):
            observation.wait()
        self.assertEqual(self.calls, ['status'])
        self.assertEqual(self.worker.client.client.timeout, 30)

    def test_preserved_session_native_pid_change_rejected_before_rpc(self):
        observation = self.observer()
        self.preserved_complete()
        self.connection['native_pid'] = 13
        self.write(self.native, self.connection)
        with self.assertRaisesRegex(ImportWaitError, 'Native process changed'):
            observation.wait()
        self.assertEqual(self.calls, [])

    def test_changed_session_reloading_still_confirms_once(self):
        observation = self.observer()
        self.complete()
        self.state['import']['state'] = 'reloading'
        self.write(self.gate, self.state)
        observation.wait()
        self.assertEqual(self.calls, ['status'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PreservedSessionTests))
    args.output.write_text(json.dumps({'passed': result.wasSuccessful(), 'tests': result.testsRun,
        'scope': 'Candidate Windows notification regression; simulated live RPC; not Unity acceptance',
        'failures': [str(x) for x in result.failures], 'errors': [str(x) for x in result.errors]}, indent=2), encoding='utf-8')
    raise SystemExit(not result.wasSuccessful())
