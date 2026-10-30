"""Windows notification safety checks. Isolated fixtures, not Unity acceptance."""
import copy
from hashlib import sha1, sha256
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from editor_wait import EditorWait, EditorWaitError, EditorWaitDeadline
from initial_wait import READINESS_SCHEMA
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'UnityNativeObserver'))
from import_notifications import write_snapshot


class EditorWaitTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.ready = self.root / 'Library/UnityWorkflowSignals/editor-readiness.json'
        self.gate = self.root / 'Library/EditorQueueGate/state.json'
        self.native = self.ready.parent / 'native-connected.json'
        self.ready.parent.mkdir(parents=True)
        self.gate.parent.mkdir(parents=True)
        self.events, self.calls, self.timeouts = [], [], []
        self.signal = {'schema': READINESS_SCHEMA, 'workspace': str(self.root), 'pid': 42,
            'epoch': 'domain-a', 'sequence': 1, 'idle': True, 'unavailable': False,
            'dirty': False, 'playing': False, 'changing': False, 'compiling': False, 'updating': False}
        self.connection = {'schema': 'native-import-notification-v1', 'workspace': str(self.root),
            'project_id': sha1((self.root / 'Assets').as_posix().encode()).hexdigest()[:16],
            'native_pid': 24, 'epoch': 'native-a', 'session_id': 'session-a'}
        self.receipt = {'id': 'job-start-play', 'state': 'acknowledged', 'sha256': 'receipt-hash',
                        'result': {'success': True}}
        self.holder = {'ticket': 7, 'owner': 'coordinator-job',
            'secretHash': sha256(b'secret').hexdigest(), 'deadline': time.time()+600,
            'blocked': None, 'operation': self.receipt}
        self.disk = {'version': 1, 'holder': self.holder, 'import': None}
        self.live = {'holder': self.holder, 'receipt': self.receipt, 'import': None,
                     'editor': dict(self.signal, playing=True)}
        self.reply = lambda: {'success': True, 'data': copy.deepcopy(self.live)}
        self.worker = SimpleNamespace(root=self.root, safe=True,
            claim_initial={'editor': {'pid': 42}},
            store=SimpleNamespace(get=lambda _: {'ticket': 7}, event=lambda *args: self.events.append(args)),
            client=SimpleNamespace(token='secret', client=SimpleNamespace(timeout=30)),
            rpc=self.rpc, require=self.require)
        self.publish()
        write_snapshot(self.gate, self.disk)
        write_snapshot(self.native, self.connection)

    def rpc(self, job, action):
        self.calls.append(action)
        self.timeouts.append(self.worker.client.client.timeout)
        return self.reply()

    @staticmethod
    def require(response):
        if response.get('success') is not True:
            raise RuntimeError('Gate refused operation')
        return response['data']

    def publish(self):
        write_snapshot(self.ready, self.signal)

    def playing(self):
        self.signal.update(playing=True, changing=True, idle=False, sequence=self.signal['sequence']+1)
        self.publish()

    def timer(self, callback, seconds=.04):
        timer = threading.Timer(seconds, callback)
        timer.start()
        self.addCleanup(timer.join)

    def observer(self):
        value = EditorWait(self.worker, 'job', 'start-play', process_check=lambda *_: True)
        self.addCleanup(value.close)
        return value

    def run_wait(self, observer, seconds=.35):
        return observer.wait(lambda editor: editor['playing'] and not editor['compiling'] and not editor['updating'], seconds)

    def test_transition_waits_without_rpc_then_confirms_once(self):
        observer = self.observer()
        self.timer(self.playing)
        self.assertTrue(self.run_wait(observer)['editor']['playing'])
        self.assertEqual(self.calls, ['status'])
        self.assertGreaterEqual(observer.wakes, 1)
        self.assertTrue(self.worker.safe)
        self.assertEqual(observer.confirmations, 1)

    def test_already_ready_hint_still_requires_live_confirmation(self):
        self.playing()
        self.assertTrue(self.run_wait(self.observer())['editor']['playing'])
        self.assertEqual(self.calls, ['status'])

    def test_stale_hint_does_not_poll_same_sequence(self):
        self.playing()
        self.live['editor']['playing'] = False
        with self.assertRaisesRegex(Exception, 'deadline'):
            self.run_wait(self.observer(), .09)
        self.assertEqual(self.calls, ['status'])
        self.assertFalse(self.worker.safe)

    def test_changed_signal_after_false_confirmation_can_confirm_again(self):
        self.playing()
        self.live['editor']['playing'] = False
        observer = self.observer()
        def later():
            self.live['editor']['playing'] = True
            self.playing()
        self.timer(later)
        self.assertTrue(self.run_wait(observer)['editor']['playing'])
        self.assertEqual(self.calls, ['status', 'status'])

    def test_reload_requires_new_native_session(self):
        observer = self.observer()
        self.signal.update(epoch='domain-b', sequence=1)
        self.playing()
        def reconnect():
            self.assertEqual(self.calls, [])
            self.connection['session_id'] = 'session-b'
            write_snapshot(self.native, self.connection)
        self.timer(reconnect)
        self.assertTrue(self.run_wait(observer)['editor']['playing'])
        self.assertEqual(self.calls, ['status'])

    def test_missing_readiness_after_command_waits_for_notification(self):
        observer = self.observer()
        self.ready.unlink()
        self.timer(self.playing)
        self.assertTrue(self.run_wait(observer)['editor']['playing'])
        self.assertEqual(self.calls, ['status'])

    def test_lease_caps_deadline_and_no_rpc_until_ready(self):
        observer = self.observer()
        self.holder['deadline'] = time.time()+.06
        write_snapshot(self.gate, self.disk)
        started = time.monotonic()
        with self.assertRaisesRegex(Exception, 'deadline'):
            self.run_wait(observer, .5)
        self.assertLess(time.monotonic()-started, .25)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worker.safe)

    def test_unrelated_events_do_not_extend_deadline(self):
        observer = self.observer()
        self.timer(lambda: write_snapshot(self.gate, self.disk))
        started = time.monotonic()
        with self.assertRaisesRegex(Exception, 'deadline'):
            self.run_wait(observer, .09)
        self.assertLess(time.monotonic()-started, .25)
        self.assertEqual(self.calls, [])

    def test_receipt_and_holder_mismatches_fail_closed_before_rpc(self):
        for field, value in [('ticket', 8), ('owner', 'foreign'), ('blocked', 'blocked'), ('secretHash', 'foreign')]:
            with self.subTest(field=field):
                old = self.holder[field]
                observer = self.observer()
                self.holder[field] = value
                write_snapshot(self.gate, self.disk)
                with self.assertRaises(EditorWaitError):
                    self.run_wait(observer)
                self.assertFalse(self.worker.safe)
                self.holder[field] = old
        self.assertEqual(self.calls, [])

    def test_receipt_pending_or_wrong_operation_is_not_replayed(self):
        for field, value in [('id', 'foreign'), ('state', 'pending'), ('sha256', '')]:
            with self.subTest(field=field):
                old = self.receipt[field]
                observer = self.observer()
                self.receipt[field] = value
                write_snapshot(self.gate, self.disk)
                with self.assertRaises(EditorWaitError):
                    self.run_wait(observer)
                self.receipt[field] = old
        self.assertEqual(self.calls, [])

    def test_live_authority_mismatch_is_not_accepted(self):
        observer = self.observer()
        self.playing()
        self.live = copy.deepcopy(self.live)
        self.live['holder']['ticket'] = 8
        with self.assertRaises(EditorWaitError):
            self.run_wait(observer)
        self.assertEqual(self.calls, ['status'])
        self.assertFalse(self.worker.safe)

    def test_confirmation_transport_failure_is_not_retried(self):
        observer = self.observer()
        self.playing()
        def failure():
            raise TimeoutError('lost response')
        self.reply = failure
        with self.assertRaises(TimeoutError):
            self.run_wait(observer)
        self.assertEqual(self.calls, ['status'])
        self.assertFalse(self.worker.safe)
        self.assertEqual(self.worker.client.client.timeout, 30)
        self.assertLessEqual(self.timeouts[0], .35)

    def test_late_live_response_is_not_accepted(self):
        observer = self.observer()
        self.playing()
        def late():
            time.sleep(.08)
            return {'success': True, 'data': copy.deepcopy(self.live)}
        self.reply = late
        with self.assertRaisesRegex(EditorWaitError, 'late'):
            self.run_wait(observer, .04)
        self.assertFalse(self.worker.safe)

    def test_publisher_pid_change_is_not_accepted(self):
        observer = self.observer()
        self.signal['pid'] = 43
        self.playing()
        with self.assertRaises(EditorWaitError):
            self.run_wait(observer)
        self.assertEqual(self.calls, [])

    def test_handles_close_after_exception(self):
        observer = self.observer()
        with self.assertRaises(RuntimeError):
            with observer:
                raise RuntimeError('command failed')
        self.assertEqual(observer.changes.handles, [])

    @unittest.skip('Experimental EditorWait-to-PhaseWorker integration is not enabled in this source snapshot')
    def test_phase_preserves_job_argument_and_delayed_stop_recovery_error(self):
        from phase import PhaseWorker, CheckFailed
        observer = self.observer()
        self.worker.editor_observation = observer
        with self.assertRaises(CheckFailed) as result:
            PhaseWorker.wait_editor(self.worker, 'job', lambda e: e['playing'], .04)
        self.assertEqual('CheckFailed: '+str(result.exception), 'CheckFailed: Editor transition deadline exceeded')
        self.assertIsInstance(result.exception.__cause__, EditorWaitDeadline)
        self.assertIsNone(self.worker.editor_observation)
        self.assertFalse(self.worker.safe)
        self.assertEqual(self.calls, [])

    @unittest.skip('Experimental EditorWait-to-PhaseWorker integration is not enabled in this source snapshot')
    def test_phase_rejects_observer_from_different_job(self):
        from phase import PhaseWorker, Uncertain
        self.worker.editor_observation = self.observer()
        with self.assertRaises(Uncertain):
            PhaseWorker.wait_editor(self.worker, 'foreign-job', lambda e: e['playing'])
        self.assertFalse(self.worker.safe)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
