"""Behavioral phase tests with isolated ledger and controllable authority failures."""
import contextlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import phase

from phase import PhaseBridge, PhaseWorker, coordinator


class Authority:
    def __init__(self):
        self.client = SimpleNamespace(timeout=30)
        self.token = None
        self.peer = True
        self.owned = False
        self.playing = False
        self.receipt = None
        self.calls = []
        self.lose_execute = None
        self.incomplete = False
        self.marker = None
        self.fail_execute_response = False
        self.pending_reads = 0

    def close(self):
        pass

    def call(self, action, **p):
        self.calls.append((action, p))
        if action == 'status':
            if self.receipt and self.pending_reads:
                self.pending_reads -= 1
                self.receipt['state'] = 'pending' if self.pending_reads else 'complete'
            return {'success': True, 'data': {'holder': {'ticket': 1} if self.owned else ({'ticket': 999} if self.peer else None),
                'receipt': self.receipt, 'editor': {'playing': self.playing or self.peer, 'changing': False,
                    'compiling': False, 'updating': False, 'idle': not (self.playing or self.peer),
                    'dirty': False, 'scene': 'Assets/Original.unity'}}}
        if action == 'request':
            self.token = 'test-secret'
            return {'success': True, 'data': {'ticket': 1, 'token': self.token}}
        if action == 'claim':
            if self.peer:
                return {'success': False, 'error': 'EDITOR_QUEUE_HELD'}
            self.owned = True
        elif action == 'release':
            if not self.owned or self.playing:
                raise AssertionError('Unsafe release')
            self.owned = False
        elif action == 'execute':
            if not self.owned or self.peer:
                raise AssertionError('Peer ownership violated')
            operation = p['operation']
            code = p['parameters'].get('code', '')
            if 'SetString("UnityWorkflow.PlayOwner"' in code:
                self.marker = operation.split('-')[0]
            if 'isPlaying=true' in code:
                self.playing = True
            if 'isPlaying=false' in code:
                if not self.marker:
                    raise AssertionError('Unowned Stop')
                self.playing = False
            inner = {'success': True, 'data': [] if p['command'] == 'read_console' else {'result': 'ok'}}
            self.receipt = {'id': operation, 'state': 'complete', 'result': inner}
            if self.lose_execute and operation.endswith(self.lose_execute):
                if self.incomplete:
                    self.receipt['state'] = 'unknown'
                if self.fail_execute_response:
                    return {'success': False, 'error': 'TimeoutError'}
                raise OSError('response lost')
            return {'success': True, 'data': {'result': inner}}
        elif action == 'ack':
            self.receipt['state'] = 'acknowledged'
        return {'success': True, 'data': {}}


class PhaseTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent / '.scratch'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name).resolve()
        self.bridge = PhaseBridge(self.root, worker_check=lambda: True, interval=.01)
        self.store = coordinator.Store(self.bridge.home)
        self.store.control('stop', '0')
        (self.bridge.home / 'installed.json').write_text('{}')
        (self.root / 'check.txt').write_text('return "ok";')
        self.authority = Authority()
        # Shared by RecoveryTests, which also invokes PhaseWorker directly.
        # Readiness/claim notification behavior is covered in separate suites.
        readiness = patch.object(phase, 'InitialReadinessWait', return_value=SimpleNamespace(
            run=lambda: self.authority.call('status')['data']))
        claim = patch.object(PhaseWorker, 'wait_for_claim', phase._worker.Worker.wait_for_claim)
        readiness.start(); claim.start()
        self.addCleanup(readiness.stop); self.addCleanup(claim.stop)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, **extra):
        manifest = {'kind': 'scene_play', 'play': True, 'play_seconds': 0,
            'edit_steps': [{'code_file': 'check.txt', 'expect': 'ok'}],
            'play_steps': [{'code_file': 'check.txt', 'expect': 'ok'}],
            'completion_steps': [{'code_file': 'check.txt', 'expect': 'ok'}], **extra}
        (self.root / 'phase.json').write_text(json.dumps(manifest))
        return self.bridge.submit('stable-key', 'phase.json')

    def process(self, job):
        self.assertEqual(job, self.store.next_job())
        worker = PhaseWorker(self.store, self.root, factory=lambda: self.authority, interval=.01)
        worker.process(job)
        return self.bridge.outcome(self.bridge.snapshot(job, evidence=True))

    def test_peer_release_then_play_cleanup_and_key_reuse(self):
        job = self.submit()
        def peer():
            time.sleep(.08)
            self.assertFalse(any(a == 'execute' for a, _ in self.authority.calls))
            self.authority.peer = False
        thread = threading.Thread(target=peer)
        thread.start()
        result = self.process(job)
        thread.join()
        self.assertTrue(result['verified_success'])
        self.assertFalse(self.authority.playing or self.authority.owned)
        self.assertIsNone(self.store.get(job)['token'])
        self.assertEqual(job, self.bridge.submit('stable-key', 'phase.json'))

    def test_lost_terminal_response_recovered_without_replay(self):
        self.authority.peer = False
        self.authority.lose_execute = 'start-play'
        result = self.process(self.submit())
        self.assertTrue(result['verified_success'])
        self.assertEqual(1, sum(a == 'execute' and p['operation'].endswith('start-play') for a, p in self.authority.calls))

    def test_unknown_response_keeps_holder_and_never_stops(self):
        self.authority.peer = False
        self.authority.lose_execute = 'start-play'
        self.authority.incomplete = True
        job = self.submit()
        result = self.process(job)
        self.assertEqual('quarantined', result['state'])
        self.assertTrue(self.authority.owned)
        self.assertFalse(any(a == 'release' or 'isPlaying=false' in p.get('parameters', {}).get('code', '') for a, p in self.authority.calls))
        self.assertIsNotNone(self.store.get(job)['token'])

    def test_returned_transport_error_recovers_terminal_receipt_without_replay(self):
        self.authority.peer = False
        self.authority.lose_execute = 'start-play'
        self.authority.fail_execute_response = True
        result = self.process(self.submit())
        self.assertTrue(result['verified_success'])
        self.assertEqual(1, sum(a == 'execute' and p['operation'].endswith('start-play') for a, p in self.authority.calls))

    def test_returned_transport_error_waits_for_pending_receipt(self):
        self.authority.peer = False
        self.authority.lose_execute = 'start-play'
        self.authority.fail_execute_response = True
        original = self.authority.call
        def call(action, **p):
            response = original(action, **p)
            if action == 'execute' and p['operation'].endswith('start-play'):
                self.authority.receipt['state'] = 'pending'
                self.authority.pending_reads = 3
            return response
        self.authority.call = call
        result = self.process(self.submit())
        self.assertTrue(result['verified_success'])
        self.assertEqual(0, self.authority.pending_reads)

    def test_failed_check_releases_known_phase(self):
        self.authority.peer = False
        result = self.process(self.submit(play_steps=[{'code_file': 'check.txt', 'expect': 'different'}]))
        self.assertEqual('failed', result['state'])
        self.assertTrue(result['snapshot']['evidence']['release_confirmed'])
        self.assertFalse(self.authority.owned or self.authority.playing)

    def test_shutdown_cancels_only_own_waiter(self):
        job = self.submit()
        self.store.control('stop', '1')
        self.assertEqual('cancelled', self.process(job)['state'])
        self.assertEqual(['request', 'cancel'], [a for a, _ in self.authority.calls if a != 'status'])
        self.assertTrue(self.authority.peer)

    def test_staged_code_is_immutable(self):
        job = self.submit()
        (self.root / 'check.txt').write_text('return "changed";')
        self.assertIn('"ok"', json.loads(self.store.get(job)['manifest'])['edit_steps'][0]['code'])
        with self.assertRaises(ValueError):
            self.bridge.submit('stable-key', 'phase.json')

    def test_reject_unknown_fields_before_request(self):
        with self.assertRaises(ValueError):
            self.submit(force_stop=True)
        self.assertFalse(self.authority.calls)

    def test_reject_play_steps_when_play_false(self):
        with self.assertRaises(ValueError):
            self.submit(play=False)

    def test_reject_unbounded_delay_and_nonfinite(self):
        for value in (121, -1, float('nan'), float('inf'), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.submit(play_seconds=value)

    def test_observer_cancel_does_not_cancel_phase(self):
        import asyncio
        job = self.submit()
        async def observe():
            task = asyncio.create_task(self.bridge.wait(job, 600))
            await asyncio.sleep(.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(observe())
        self.authority.peer = False
        self.assertTrue(self.process(job)['verified_success'])


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PhaseTests))
    args.output.write_text(json.dumps({'passed': result.wasSuccessful(), 'tests': result.testsRun,
        'failures': [str(x) for x in result.failures], 'errors': [str(x) for x in result.errors]}, indent=2))
    raise SystemExit(not result.wasSuccessful())
