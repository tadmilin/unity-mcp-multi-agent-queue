"""A failed Editor command must keep its compiler lines in the job reason."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import phase
from phase import PhaseWorker, command_failure
import test_phase


class CompileFailAuthority(test_phase.Authority):
    def call(self, action, **p):
        if action == 'execute' and 'BROKEN' in p['parameters'].get('code', ''):
            if not self.owned or self.peer:
                raise AssertionError('Peer ownership violated')
            inner = {'success': False, 'error': 'Compilation failed',
                     'data': {'compiler': 'codedom', 'errors': ['Line 3: boom', 'Line 9: bang']}}
            self.receipt = {'id': p['operation'], 'state': 'complete', 'result': inner}
            return {'success': True, 'data': {'result': inner}}
        return super().call(action, **p)


class PollClaimWorker(PhaseWorker):
    # The fake authority has no readiness files; claim_wait has its own tests.
    wait_for_claim = phase._worker.Worker.wait_for_claim


class CommandFailureTests(unittest.TestCase):
    def test_compiler_lines_kept(self):
        reason = command_failure({'success': False, 'error': 'Compilation failed',
                                  'data': {'errors': ['Line 3: boom', 'Line 9: bang']}})
        self.assertEqual('Command failed: Compilation failed: Line 3: boom | Line 9: bang', reason)

    def test_without_errors_unchanged(self):
        self.assertEqual('Command failed: Runtime error: x',
                         command_failure({'success': False, 'error': 'Runtime error: x', 'data': None}))
        self.assertEqual('Command failed: busy', command_failure({'success': False, 'message': 'busy'}))

    def test_reason_capped(self):
        reason = command_failure({'success': False, 'error': 'Compilation failed', 'data': {'errors': ['x' * 5000]}})
        self.assertEqual(len('Command failed: ') + 1200, len(reason))


class CompileFailurePhaseTests(unittest.TestCase):
    setUp = test_phase.PhaseTests.setUp
    tearDown = test_phase.PhaseTests.tearDown
    submit = test_phase.PhaseTests.submit

    def test_failed_compile_reason_reaches_result_and_releases(self):
        (self.root / 'broken.txt').write_text('return BROKEN;')
        self.authority = CompileFailAuthority()
        self.authority.client = SimpleNamespace(timeout=15)
        self.authority.peer = False
        job = self.submit(edit_steps=[{'code_file': 'broken.txt', 'expect': 'ok'}],
                          completion_steps=[{'code_file': 'check.txt', 'expect': 'ok'}])
        self.assertEqual(job, self.store.next_job())
        # This fake has no OS publisher. Real notification/process checks have their own suite.
        with patch('phase.InitialReadinessWait') as initial:
            initial.return_value.run.return_value = {'editor': {'pid': 22}}
            PollClaimWorker(self.store, self.root, factory=lambda: self.authority, interval=.01).process(job)
        result = self.bridge.outcome(self.bridge.snapshot(job, evidence=True))
        self.assertEqual('failed', result['state'])
        self.assertIn('Compilation failed: Line 3: boom | Line 9: bang', json.dumps(result))
        self.assertTrue(result['snapshot']['evidence']['release_confirmed'])
        self.assertFalse(self.authority.owned or self.authority.playing)


if __name__ == '__main__':
    unittest.main()
