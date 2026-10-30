"""Notification and deadline safety tests; these are not Unity acceptance."""
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import sys
from types import SimpleNamespace
import unittest

from claim_wait import ClaimWait
from initial_wait import InitialReadinessWait, InitialReadinessError, READINESS_SCHEMA, live_unity_process
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'UnityNativeObserver'))
from import_notifications import write_snapshot


class InitialWaitTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.ready = self.root / 'Library/UnityWorkflowSignals/editor-readiness.json'
        self.home = self.root / 'JobQueue/phases'
        for folder in (self.ready.parent, self.home, self.root / 'Library/EditorQueueGate'):
            folder.mkdir(parents=True, exist_ok=True)
        self.events, self.stop = [], '0'
        self.signal = {'schema': READINESS_SCHEMA, 'workspace': str(self.root), 'pid': 22,
            'epoch': 'boot', 'sequence': 1, 'idle': True, 'unavailable': False,
            'dirty': False, 'playing': False, 'changing': False, 'compiling': False, 'updating': False}
        self.worker = SimpleNamespace(root=self.root, timeout=10,
            store=SimpleNamespace(home=self.home, control=lambda key: self.stop,
                                  event=lambda *args: self.events.append(args)))
        self.publish()

    def publish(self):
        # Match Unity File.Replace/ReplaceFile, not MoveFileEx(os.replace),
        # which has different shared-reader behavior on Windows.
        write_snapshot(self.ready, self.signal)

    def wait(self, callback=None, budget=.5, process_check=lambda *_: True):
        observer = InitialReadinessWait(self.worker, 'job', time.monotonic()+budget,
                                        process_check=process_check)
        if callback:
            timer = threading.Timer(.04, callback)
            timer.start()
            self.addCleanup(timer.join)
        return observer.run()

    def test_busy_publisher_does_not_postpone_fifo_request(self):
        self.signal.update(playing=True, changing=True, idle=False)
        self.publish()
        self.assertEqual(self.wait(), {'editor': {'pid': 22}})
        self.assertIs(self.events[-1][2]['idle_hint'], False)

    def test_reload_waits_for_notification_without_rpc(self):
        self.signal.update(unavailable=True, idle=False)
        self.publish()
        def reloaded():
            self.signal.update(unavailable=False, idle=True, epoch='new-domain', sequence=1)
            self.publish()
        self.assertEqual(self.wait(reloaded)['editor']['pid'], 22)
        self.assertEqual(self.events[-1][2]['epoch'], 'new-domain')
        self.assertGreaterEqual(self.events[-1][2]['notification_wakes'], 1)

    def test_absent_publisher_waits_for_creation(self):
        self.ready.unlink()
        self.assertEqual(self.wait(self.publish)['editor']['pid'], 22)

    def test_dead_process_waits_for_new_publisher(self):
        def replaced():
            self.signal.update(pid=23, epoch='replacement')
            self.publish()
        self.assertEqual(self.wait(replaced, process_check=lambda pid, *_: pid == 23)['editor']['pid'], 23)

    def test_mismatched_workspace_fails_without_ticket(self):
        self.signal['workspace'] = str(self.root / 'different')
        self.publish()
        with self.assertRaises(InitialReadinessError):
            self.wait()

    def test_invalid_identity_fields_fail(self):
        for field, value in [('pid', True), ('pid', 0), ('pid', 2**32),
                             ('sequence', 0), ('epoch', ''), ('idle', 1), ('schema', 'old')]:
            with self.subTest(field=field, value=value):
                previous = self.signal[field]
                self.signal[field] = value
                self.publish()
                with self.assertRaises(InitialReadinessError):
                    self.wait()
                self.signal[field] = previous

    def test_deadline_stays_fixed_despite_notifications(self):
        self.signal.update(unavailable=True, idle=False)
        self.publish()
        start = time.monotonic()
        with self.assertRaisesRegex(InitialReadinessError, 'deadline'):
            self.wait(self.publish, budget=.12)
        self.assertLess(time.monotonic()-start, .4)

    def test_stop_wakes_without_creating_ticket(self):
        self.signal.update(unavailable=True, idle=False)
        self.publish()
        def stop():
            self.stop = '1'
            (self.home / 'control-event').write_text('stop')
        with self.assertRaisesRegex(InitialReadinessError, 'Stopped'):
            self.wait(stop)

    def test_claim_cannot_reset_pre_request_budget(self):
        gate = self.root / 'Library/EditorQueueGate/state.json'
        gate.write_text(json.dumps({'version': 1, 'holder': {'ticket': 1}, 'waiting': [
            {'ticket': 2, 'owner': 'coordinator-job', 'secretHash': sha256(b'secret').hexdigest(),
             'deadline': time.time()+1800}]}))
        calls, finishes = [], []
        self.worker.store.get = lambda job: {'ticket': 2}
        self.worker.client = SimpleNamespace(token='secret')
        self.worker.rpc = lambda job, action: calls.append(action) or {'success': True}
        self.worker.require = lambda response: None
        self.worker.finish = lambda *args: finishes.append(args)
        deadline = time.monotonic()+.06
        start = time.monotonic()
        observation = ClaimWait(self.worker, 'job', {'editor': {'pid': 22}}, deadline=deadline)
        self.assertFalse(observation.run())
        self.assertEqual(calls, ['cancel'])
        self.assertEqual(finishes[0][1], 'cancelled')
        self.assertLess(time.monotonic()-start, .3)

    def test_windows_process_check_rejects_python_and_absent_pid(self):
        self.assertFalse(live_unity_process(os.getpid(), time.time_ns()))
        self.assertFalse(live_unity_process(0xffffffff, time.time_ns()))


if __name__ == '__main__':
    unittest.main()
