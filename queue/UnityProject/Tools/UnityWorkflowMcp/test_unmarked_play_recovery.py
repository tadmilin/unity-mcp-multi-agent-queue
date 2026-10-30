"""Actual phase/ledger fail path: Edit-only task enters external Play at cleanup."""
import json
import base64
import hashlib
from unittest.mock import patch
import unittest

from test_phase import PhaseTests
from test_recovery import RecoveryAuthority
from phase import PhaseWorker


class ExternalPlayAuthority(RecoveryAuthority):
    def __init__(self):
        super().__init__()
        self.inject_play = True
        self.scene = 'Assets/Original.unity'
        self.pid = 123
        self.import_data = None
        self.target_scene = None

    def call(self, action, **p):
        response = super().call(action, **p)
        if action == 'status':
            response['data']['editor'].update(scene=self.scene, pid=self.pid)
            response['data']['import'] = self.import_data
        if action == 'write_files':
            self.receipt=dict(id=p['operation'],state='complete',result={'success':True})
        if action == 'ack' and p.get('operation', '').endswith('-completion-0') and self.inject_play:
            self.playing = True
            self.inject_play = False
        if action == 'execute' and '-unmarked-recovery-inspect-' in p.get('operation', ''):
            # Model only the read-only C# probe's observations, never change them.
            value = 'blocked' if self.marker or self.playing or self.changing or self.dirty or self.scene not in ('Assets/Original.unity',self.target_scene) else 'unmarked-play-ended-clean-idle'
            self.receipt['result']['data']['result'] = value
        if action == 'execute' and p.get('operation','').endswith('-reopen-from-disk'):
            self.receipt['result']['data']['result'] = 'reopened-from-disk'
        if action == 'execute' and p.get('operation','').endswith('-load-scene') and self.target_scene:
            self.scene=self.target_scene
        if action == 'execute' and p.get('operation','').endswith('-unmarked-recovery-restore-scene'):
            self.scene='Assets/Original.unity';self.receipt['result']['data']['result']='restored'
        return response


class UnmarkedPlayRecoveryTests(unittest.TestCase):
    setUp = PhaseTests.setUp
    tearDown = PhaseTests.tearDown
    submit = PhaseTests.submit

    def stranded(self, files=False, scene_save=None, checked=False):
        self.authority = ExternalPlayAuthority()
        extra={"kind":"check"} if checked else {}
        if files:
            (self.root/'payload.txt').write_text('verified-payload')
            extra=dict(kind='files',files=[dict(path='Assets/probe.txt',source='payload.txt',expectedSha256='missing')])
        if scene_save is not None:
            (self.root/'Assets').mkdir(exist_ok=True)
            for name in ['Original','Target','Away']:(self.root/'Assets'/(name+'.unity')).write_text('saved-scene')
            digest=hashlib.sha256(b'saved-scene').hexdigest()
            extra=dict(kind='scene_save',save_scene=scene_save,
                scene=dict(path='Assets/Target.unity',expectedSha256=digest),
                away_scene=dict(path='Assets/Away.unity',expectedSha256=digest))
            self.authority.target_scene='Assets/Target.unity'
        job = self.submit(play=False, play_steps=[],**extra)
        self.assertEqual(job, self.store.next_job())
        worker = PhaseWorker(self.store, self.root, factory=lambda: self.authority, interval=.001)
        if files:
            # Import transport/hash/boot behavior is tested by test_import_wait.
            # This seam provides the proven import and real ledger for cleanup.
            def imported(job, manifest):
                worker.files_match(manifest)
                worker.rpc(job,'write_files',operation=job+'-write',files=manifest['files'])
                worker.rpc(job,'ack',operation=job+'-write');worker.rpc(job,'import')
                target=self.root/'Assets/probe.txt';target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(base64.b64decode(manifest['files'][0]['base64']))
                worker.files_match(manifest,after=True)
                self.authority.import_data=dict(state='complete',operation=job+'-write')
                self.store.event(job,'import_wait_confirmed',dict(operation=job+'-write',ticket=1,bootUtc='new-domain'))
            with patch.object(worker,'import_files',imported):worker.process(job)
        else:worker.process(job)
        row = self.store.get(job)
        self.assertEqual('quarantined', row['state'])
        self.assertIn('Unmarked Play; refused to stop', row['result'])
        self.assertTrue(self.authority.owned and self.authority.playing)
        self.assertFalse(any('isPlaying=false' in p.get('parameters', {}).get('code', '') for _,p in self.authority.calls))
        self.authority.playing = False  # External Play ends; worker never stops it.
        return worker, job

    def assert_no_workload_replay_or_scene_change(self, owned_restore=False):
        code = '\n'.join(p.get('parameters', {}).get('code', '') for action,p in self.authority.calls if action == 'execute')
        self.assertNotIn('isPlaying=false', code)
        self.assertNotIn('EraseString', code)
        recovery_code='\n'.join(p.get('parameters',{}).get('code','') for _,p in self.authority.calls if '-unmarked-recovery-' in p.get('operation',''))
        if owned_restore:self.assertEqual(1,recovery_code.count('.OpenScene('))
        else:self.assertNotIn('OpenScene', recovery_code)
        self.assertNotIn('SaveScene', recovery_code)
        for suffix in ('edit-0','completion-0'):
            self.assertEqual(1,sum(action=='execute' and p.get('operation','').endswith('-'+suffix) for action,p in self.authority.calls))

    def test_checked_read_only_task_recovers_same_external_play_cleanup(self):
        worker, job = self.stranded(checked=True)
        self.assertTrue(worker.recover_quarantined(job))
        self.assertEqual("abandoned", self.store.get(job)["state"])
        self.assertFalse(self.authority.owned)
        self.assert_no_workload_replay_or_scene_change()

    def test_original_edit_only_external_play_releases_when_clean_and_stays_unsuccessful(self):
        worker, job = self.stranded()
        self.assertTrue(worker.recover_quarantined(job))
        row = self.store.get(job)
        self.assertEqual('abandoned', row['state'])
        self.assertIsNone(row['token'])
        self.assertFalse(self.authority.owned)
        self.assertFalse(self.bridge.outcome(self.bridge.snapshot(job))['verified_success'])
        self.assert_no_workload_replay_or_scene_change()

    def test_busy_dirty_and_wrong_scene_wait_without_mutation(self):
        worker, job = self.stranded()
        for name,value in [('playing',True),('changing',True),('dirty',True),('scene','Assets/Other.unity')]:
            with self.subTest(name=name):
                old=getattr(self.authority,name);setattr(self.authority,name,value)
                start=len(self.authority.calls)
                self.assertFalse(worker.recover_quarantined(job))
                self.assertTrue(all(a=='status' for a,_ in self.authority.calls[start:]))
                setattr(self.authority,name,old)

    def test_foreign_owner_never_touched(self):
        worker, job = self.stranded(); self.authority.owner = 'another-owner'
        start=len(self.authority.calls)
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(all(a=='status' for a,_ in self.authority.calls[start:]))

    def test_foreign_marker_not_erased_or_released(self):
        worker, job = self.stranded(); self.authority.marker='another-play'
        self.assertFalse(worker.recover_quarantined(job))
        self.assertEqual('another-play',self.authority.marker)
        self.assertTrue(self.authority.owned)
        self.authority.marker=None
        self.assertTrue(worker.recover_quarantined(job))
        self.assert_no_workload_replay_or_scene_change()

    def test_unknown_receipt_never_recovered(self):
        worker, job = self.stranded(); self.authority.receipt['state']='unknown'
        start=len(self.authority.calls)
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(all(a=='status' for a,_ in self.authority.calls[start:]))

    def test_missing_completion_proof_refused(self):
        worker, job = self.stranded()
        self.store.db.execute("DELETE FROM events WHERE job=? AND kind='check'",(job,));self.store.db.commit()
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(self.authority.owned)

    def test_editor_process_changed_refused(self):
        worker, job = self.stranded(); self.authority.pid=124
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(self.authority.owned)

    def test_expired_same_owner_recovers_without_stop(self):
        worker, job = self.stranded(); self.authority.blocked='owner_deadline_expired'
        self.assertTrue(worker.recover_quarantined(job))
        self.assertEqual(1,sum(a=='recover_expired' for a,_ in self.authority.calls))
        self.assert_no_workload_replay_or_scene_change()

    def test_lost_release_reply_reconciles_without_second_release(self):
        worker, job = self.stranded(); self.authority.drop_release=True
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(worker.recover_quarantined(job))
        self.assertEqual(1,sum(a=='release' for a,_ in self.authority.calls))
        self.assert_no_workload_replay_or_scene_change()

    def test_unknown_inspection_receipt_is_not_replayed(self):
        worker, job = self.stranded();self.authority.lose_execute='unmarked-recovery-inspect-0';self.authority.incomplete=True
        self.assertFalse(worker.recover_quarantined(job))
        self.assertFalse(worker.recover_quarantined(job))
        self.assertEqual(1,sum(a=='execute' and p['operation'].endswith('unmarked-recovery-inspect-0') for a,p in self.authority.calls))
        self.assertTrue(self.authority.owned)

    def test_completed_import_then_external_play_releases_without_reimport(self):
        worker,job=self.stranded(files=True)
        self.assertTrue(worker.recover_quarantined(job))
        self.assertEqual('abandoned',self.store.get(job)['state'])
        self.assertEqual(1,sum(a=='write_files' for a,_ in self.authority.calls))
        self.assertEqual(1,sum(a=='import' for a,_ in self.authority.calls))
        self.assert_no_workload_replay_or_scene_change()

    def test_import_unknown_or_changed_payload_keeps_ownership(self):
        worker,job=self.stranded(files=True)
        self.authority.import_data['state']='unknown'
        self.assertFalse(worker.recover_quarantined(job))
        self.authority.import_data['state']='complete'
        (self.root/'Assets/probe.txt').write_text('different')
        self.assertFalse(worker.recover_quarantined(job))
        self.assertTrue(self.authority.owned)

    def test_scene_save_completed_then_external_play_releases_without_resave(self):
        worker,job=self.stranded(scene_save=True)
        self.assertTrue(worker.recover_quarantined(job))
        self.assertEqual('abandoned',self.store.get(job)['state'])
        self.assertEqual(1,sum(a=='execute' and p['operation'].endswith('-save-scene') for a,p in self.authority.calls))
        self.assert_no_workload_replay_or_scene_change(owned_restore=True)

    def test_scene_reopen_without_save_recovers_but_changed_scene_is_refused(self):
        worker,job=self.stranded(scene_save=False)
        (self.root/'Assets/Target.unity').write_text('changed-by-another-writer')
        self.assertFalse(worker.recover_quarantined(job));self.assertTrue(self.authority.owned)
        (self.root/'Assets/Target.unity').write_text('saved-scene')
        self.assertTrue(worker.recover_quarantined(job))
        self.assert_no_workload_replay_or_scene_change(owned_restore=True)


if __name__=='__main__':unittest.main()
