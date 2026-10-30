"""Resume only proven delayed-Stop cleanup. No Play/Stop/import/workload replay."""
import json
import time


class CleanupRecovery:
    def __init__(self, worker, job):
        self.worker, self.store, self.job = worker, worker.store, job

    def events(self, kind):
        return [json.loads(row[0]) for row in self.store.db.execute(
            'SELECT detail FROM events WHERE job=? AND kind=? ORDER BY seq', (self.job, kind))]

    def note(self, reason):
        entries = self.events('cleanup_recovery_wait')
        if not entries or entries[-1].get('reason') != reason:
            self.store.event(self.job, 'cleanup_recovery_wait', {'reason': reason})
        return False

    def step(self, label, code=None, command='execute_code', parameters=None):
        operation = self.job + '-' + label
        done = [e for e in self.events('cleanup_recovery_step') if e['operation'] == operation]
        if done:
            return done[-1]['result']
        intents = [e for e in self.events('intent') if e.get('operation') == operation]
        if intents:
            # A crash can happen after dispatch and before our durable checkpoint.
            # Only the original terminal receipt can finish that step.
            receipt = self.worker.status(self.job, owner=True).get('receipt') or {}
            if receipt.get('id') != operation or receipt.get('state') not in ('complete', 'acknowledged'):
                raise RuntimeError('Recovery step outcome unknown; no replay')
            result = receipt.get('result') or {}
            if result.get('success') is not True:
                raise RuntimeError('Recovery step failed; no replay')
            if receipt['state'] == 'complete':
                self.worker.require(self.worker.rpc(self.job, 'ack', operation=operation))
        else:
            result = self.worker.command(self.job, label, code, command, parameters)
        self.store.event(self.job, 'cleanup_recovery_step', {'operation': operation, 'result': result})
        return result

    def run(self):
        row = self.store.get(self.job)
        if row['state'] != 'quarantined' or not row['token']:
            return False
        from bridge import coordinator
        manifest = json.loads(row['manifest'])
        if (manifest.get('kind') != 'scene_play' or not manifest.get('play') or
                coordinator.digest(coordinator.encode(manifest).encode()) != row['digest']):
            return self.note('Manifest identity is not a verified scene/Play phase')
        plans = self.events('cleanup_recovery_plan')
        original_result = json.loads(row['result'] or '{}')
        if not plans and 'cleanup: CheckFailed: Editor transition deadline exceeded' not in original_result.get('reason', ''):
            return self.note('Quarantine is outside delayed-Stop cleanup scope')
        worker = self.worker
        try:
            worker.client = worker.factory()
            worker.client.token = row['token']
            # One observation per service tick, no per-job blocking status loop.
            state = worker.status(self.job, owner=False)
            holder = state.get('holder') or {}
            release_intents = [e for e in self.events('intent') if e.get('action') == 'release']
            if plans and release_intents and holder.get('ticket') != row['ticket']:
                return self.finish(plans[-1], {'owned_holder_absent_after_release_intent': True})
            if holder.get('ticket') != row['ticket'] or holder.get('owner') != 'coordinator-' + self.job:
                return self.note('Holder identity differs; no cross-owner action')
            state = worker.status(self.job, owner=True)
            holder, editor, receipt = state.get('holder') or {}, state.get('editor') or {}, state.get('receipt') or {}
            if holder.get('ticket') != row['ticket'] or holder.get('owner') != 'coordinator-' + self.job:
                return self.note('Authenticated holder identity differs')
            if (not editor.get('idle') or any(editor.get(k) for k in ('playing', 'changing', 'compiling', 'updating', 'dirty'))
                    or state.get('import') is not None):
                return self.note('Waiting for actual clean idle Editor; never replay Stop')
            if holder.get('blocked') not in (None, '', 'owner_deadline_expired'):
                return self.note('Authority has an unresolved block')
            allowed = [self.job + '-' + s for s in ('stop-owned-play', 'recovery-check-marker',
                       'recovery-clear-marker', 'recovery-restore-scene', 'recovery-console')]
            if (receipt.get('id') not in (allowed if plans else allowed[:1])
                    or receipt.get('state') not in (('complete', 'acknowledged') if plans else ('acknowledged',))
                    or (receipt.get('result') or {}).get('success') is not True):
                return self.note('Terminal successful owned cleanup receipt is missing')
            if not plans:
                if (receipt['result'].get('data') or {}).get('result') != 'stop-requested':
                    return self.note('Stop receipt does not confirm the owned Stop request')
                initial = [s for s in self.events('status') if
                           (s.get('holder') or {}).get('ticket') == row['ticket']
                           and (s.get('holder') or {}).get('owner') == 'coordinator-' + self.job
                           and (s.get('editor') or {}).get('idle')]
                if not initial or not initial[0]['editor'].get('scene'):
                    return self.note('Original owned clean scene evidence missing')
                plan = {'original_result': original_result, 'manifest_digest': row['digest'],
                        'ticket': row['ticket'], 'original_scene': initial[0]['editor']['scene'],
                        'stop_receipt': receipt['id'], 'created': time.time()}
                self.store.event(self.job, 'cleanup_recovery_plan', plan)
            else:
                plan = plans[-1]
                if plan['manifest_digest'] != row['digest'] or plan['ticket'] != row['ticket']:
                    return self.note('Recovery plan identity changed')
            if holder.get('blocked') == 'owner_deadline_expired':
                worker.require(worker.rpc(self.job, 'recover_expired'))
            marker = self.step('recovery-check-marker',
                '// recovery marker\n'
                'if(UnityEditor.SessionState.GetString("UnityWorkflow.PlayOwner", "")!=' + json.dumps(self.job)
                + ') throw new System.Exception("Recovery Play owner mismatch"); return "owned";')
            if (marker.get('data') or {}).get('result') != 'owned':
                return self.note('Recovery marker assertion did not return owned')
            self.step('recovery-clear-marker',
                'if(UnityEditor.SessionState.GetString("UnityWorkflow.PlayOwner", "")!=' + json.dumps(self.job)
                + ') throw new System.Exception("Recovery Play owner mismatch"); '
                'UnityEditor.SessionState.EraseString("UnityWorkflow.PlayOwner"); return "cleared";')
            if editor['scene'] != plan['original_scene']:
                self.step('recovery-restore-scene',
                    'if(UnityEditor.EditorApplication.isPlayingOrWillChangePlaymode) throw new System.Exception("Editor busy"); '
                    'UnityEditor.SceneManagement.EditorSceneManager.OpenScene(' + json.dumps(plan['original_scene'])
                    + ', UnityEditor.SceneManagement.OpenSceneMode.Single); return "restored";')
            console = self.step('recovery-console', command='read_console',
                                parameters={'action': 'get', 'types': ['error'], 'count': 20})
            state = worker.status(self.job, owner=True)
            if (not state['editor']['idle'] or state['editor']['scene'] != plan['original_scene']
                    or state['editor'].get('dirty') or (state.get('holder') or {}).get('ticket') != row['ticket']):
                return self.note('Final owned scene is not clean idle; release refused')
            if release_intents:
                return self.note('Prior release outcome unresolved; no release replay')
            worker.require(worker.rpc(self.job, 'release'))
            return self.finish(plan, {'release_confirmed': True, 'console': console,
                                      'editor_before_release': state['editor']})
        except Exception as error:
            return self.note(type(error).__name__ + ': ' + str(error)[:300])
        finally:
            if worker.client:
                worker.client.close()
                worker.client = None

    def finish(self, plan, evidence):
        self.store.event(self.job, 'cleanup_recovered', evidence)
        self.worker.finish(self.job, 'abandoned', {
            'reason': 'Delayed Stop cleanup recovered; original phase remains unsuccessful',
            'original_result': plan['original_result'], 'cleanup_recovered': True,
            'original_scene': plan['original_scene'],
            'editor_before_release': evidence.get('editor_before_release'),
            'console': evidence.get('console'),
            'recovery_evidence': evidence}, True)
        return True
