"""Exercise delayed Stop cleanup through the real phase worker and isolated store."""
import json
import asyncio
import unittest
from unittest.mock import patch
from test_phase import PhaseTests, Authority
from phase import PhaseWorker, CheckFailed
from bridge import Bridge, coordinator


class RecoveryAuthority(Authority):
    def __init__(self):
        super().__init__()
        self.peer = False
        self.owner = None
        self.blocked = None
        self.dirty = False
        self.changing = False
        self.drop_release = False

    def call(self, action, **p):
        if action == 'request':
            self.owner = p['owner']
        if action == 'recover_expired':
            self.blocked = None
        code = p.get('parameters', {}).get('code', '')
        if action == 'execute' and 'recovery marker' in code:
            expected = self.owner.removeprefix('coordinator-')
            if self.marker != expected:
                return {'success': False, 'error': 'marker mismatch'}
        answer = super().call(action, **p)
        if action == 'status':
            data = answer['data']
            if self.owned:
                data['holder'].update(owner=self.owner, blocked=self.blocked)
            data['editor'].update(dirty=self.dirty, changing=self.changing)
            data['editor']['idle'] = not (self.playing or self.peer or self.dirty or self.changing)
            data['import'] = None
        if action == 'execute' and ('isPlaying=false' in code or (p['command'] == 'workflow_play' and p['parameters']['action'] == 'stop')):
            self.receipt['result']['data']['result'] = 'stop-requested'
        if action == 'execute' and 'recovery marker' in code:
            self.receipt['result']['data']['result'] = 'owned'
        if action == 'execute' and 'EraseString' in code:
            self.marker = None
        if action == 'release' and self.drop_release:
            raise OSError('release reply lost')
        return answer


class RecoveryTests(unittest.TestCase):
    setUp = PhaseTests.setUp
    tearDown = PhaseTests.tearDown
    submit = PhaseTests.submit

    def stranded(self):
        self.authority = RecoveryAuthority()
        job = self.submit()
        self.store.next_job()
        worker = PhaseWorker(self.store, self.root, factory=lambda: self.authority, interval=.001)
        original = worker.wait_editor
        count = 0
        def wait(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise CheckFailed('Editor transition deadline exceeded')
            return original(*args, **kwargs)
        with patch.object(worker, 'wait_editor', wait):
            worker.process(job)
        self.assertEqual('quarantined', self.store.get(job)['state'])
        self.assertTrue(self.authority.owned)
        return worker, job

    def test_delayed_stop_idle_is_released_without_replaying_stop(self):
        worker, job = self.stranded()
        self.assertTrue(worker.recover_quarantined(job))
        row = self.store.get(job)
        self.assertEqual('abandoned', row['state'])
        self.assertIsNone(row['token'])
        self.assertFalse(self.authority.owned)
        self.assertEqual(1, sum(a == 'execute' and p['operation'].endswith('stop-owned-play') for a, p in self.authority.calls))
        self.assertIn('Editor transition deadline exceeded', row['result'])
        outcome = self.bridge.outcome(self.bridge.snapshot(job, evidence=True))
        self.assertTrue(outcome['cleanup_recovered'])
        self.assertFalse(outcome['verified_success'])
        self.assertTrue(outcome['scene_restored'] and outcome['play_stopped'])

    def test_expired_idle_owner_uses_authority_recovery(self):
        worker, job = self.stranded()
        self.authority.blocked = 'owner_deadline_expired'
        self.assertTrue(worker.recover_quarantined(job))
        self.assertEqual(1, sum(a == 'recover_expired' for a, _ in self.authority.calls))

    def test_busy_editor_is_observed_only_then_recovered_when_idle(self):
        worker, job = self.stranded()
        self.authority.changing = True
        before = len(self.authority.calls)
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(all(a == 'status' for a, _ in self.authority.calls[before:]))
        self.authority.changing = False
        self.assertTrue(worker.recover_quarantined(job))

    def test_foreign_owner_is_never_touched(self):
        worker, job = self.stranded()
        self.authority.owner = 'coordinator-other'
        before = len(self.authority.calls)
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(all(a == 'status' for a, _ in self.authority.calls[before:]))

    def test_unknown_receipt_is_never_recovered(self):
        worker, job = self.stranded()
        self.authority.receipt['state'] = 'unknown'
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(self.authority.owned)

    def test_foreign_marker_keeps_quarantine(self):
        worker, job = self.stranded()
        self.authority.marker = 'foreign'
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(self.authority.owned)

    def test_dirty_scene_keeps_quarantine(self):
        worker, job = self.stranded()
        self.authority.dirty = True
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(self.authority.owned)

    def test_lost_release_reply_is_reconciled_without_replay(self):
        worker, job = self.stranded()
        self.authority.drop_release = True
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(worker.recover_quarantined(job))
        self.assertEqual(1, sum(a == 'release' for a, _ in self.authority.calls))

    def test_crash_after_clear_marker_resumes_original_receipt(self):
        worker, job = self.stranded()
        event = self.store.event
        def crash(job_id, kind, detail):
            if kind == 'cleanup_recovery_step' and detail['operation'].endswith('recovery-clear-marker'):
                raise OSError('Checkpoint interruption')
            return event(job_id, kind, detail)
        with patch.object(self.store, 'event', crash):
            self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(worker.recover_quarantined(job))
        self.assertEqual(1, sum(a == 'execute' and p['operation'].endswith('recovery-clear-marker') for a, p in self.authority.calls))

    def test_cross_executor_quarantine_refuses_before_new_submission(self):
        self.stranded()
        bridge = Bridge(self.root, worker_check=lambda: True)
        store = coordinator.Store(bridge.home)
        try:
            (bridge.home / 'installed.json').write_text('{}')
            (self.root / 'check.json').write_text('{"kind":"check"}')
            with self.assertRaisesRegex(ValueError, 'EDITOR_QUEUE_QUARANTINE_BLOCKED'):
                bridge.submit('file-waiter', 'check.json')
            self.assertEqual(0, store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
        finally:
            store.close()

    def test_already_queued_job_reports_blocker_without_900_second_wait(self):
        bridge = Bridge(self.root, worker_check=lambda: True)
        store = coordinator.Store(bridge.home)
        try:
            manifest = {'kind': 'check', 'files': [], 'depends_on': []}
            job = store.submit('existing', manifest)
            self.stranded()
            result = asyncio.run(bridge.wait(job, 900))
            self.assertEqual('EDITOR_QUEUE_QUARANTINE_BLOCKED', result['reason'])
            self.assertTrue(result['blocked_by'])
            self.assertFalse(result['job_cancelled_by_observer'])
            self.assertEqual('queued', store.get(job)['state'])
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
