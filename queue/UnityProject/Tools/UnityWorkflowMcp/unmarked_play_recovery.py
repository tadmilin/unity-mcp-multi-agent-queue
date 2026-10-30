"""Service owner releases a finished Edit-only phase after external Play ends.

Never Stop, save, erase a Play marker or repeat task commands. A clean scene
explicitly managed by the phase may be restored to its recorded original scene.
The original task remains unsuccessful. Other owners and uncertain receipts stay
untouched. All credentials remain inside the existing central worker.
"""
from hashlib import sha256
import json
from pathlib import Path
import time

from recovery import CleanupRecovery

VERSION = 'owned-terminal-idle-v4'
LOADED_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()
REASON = '; cleanup: Uncertain: Unmarked Play; refused to stop'


class UnmarkedPlayRecovery(CleanupRecovery):
    @staticmethod
    def matches(row):
        return json.loads(row['result'] or '{}').get('reason') == REASON

    def origin(self, row):
        """Durable proof, independent of current Editor observations."""
        from bridge import coordinator
        manifest = json.loads(row['manifest'])
        if (manifest.get('kind') not in ('scene_play','files','scene_save','check') or manifest.get('play') is not False
                or manifest.get('play_steps')
                or not manifest.get('completion_steps')
                or coordinator.digest(coordinator.encode(manifest).encode()) != row['digest']):
            raise ValueError('Not an unchanged completed Edit-only manifest')
        expected = [(stage, index, step['expect']) for stage in ('edit', 'completion')
                    for index, step in enumerate(manifest[stage + '_steps'])]
        checks = self.events('check')
        if [(c.get('stage'), c.get('index'), c.get('expected')) for c in checks] != expected or any(
                c.get('actual') != c.get('expected') for c in checks):
            raise ValueError('Every declared edit/completion check must have passed')
        original = next((s for s in self.events('status') if
            (s.get('holder') or {}).get('ticket') == row['ticket'] and
            (s.get('holder') or {}).get('owner') == 'coordinator-' + self.job and
            (s.get('editor') or {}).get('idle')), None)
        editor = (original or {}).get('editor') or {}
        if not editor.get('scene') or type(editor.get('pid')) is not int or editor['pid'] <= 0:
            raise ValueError('Original owned scene and Editor process evidence missing')
        end = self.job + '-completion-' + str(len(manifest['completion_steps']) - 1)
        operations = [self.job + '-preflight']
        if manifest.get('scene'):
            create = manifest['kind']=='scene_save' and manifest['scene']['expectedSha256']=='missing'
            operations.append(self.job + ('-create-scene' if create else '-load-scene'))
        operations += [self.job + '-edit-' + str(i) for i in range(len(manifest['edit_steps']))]
        saved_scene = None
        if manifest['kind']=='scene_save':
            if manifest['save_scene']: operations.append(self.job+'-save-scene')
            operations.append(self.job+'-reopen-from-disk')
            reopened = self.events('scene_reopened')
            if (len(reopened)!=1 or reopened[0].get('path')!=manifest['scene']['path']
                    or reopened[0].get('away_scene')!=manifest['away_scene']['path']
                    or reopened[0].get('save_requested') is not manifest['save_scene']):
                raise ValueError('Completed scene-save/reopen proof missing')
            saved_scene = {'path':reopened[0]['path'], 'sha256':reopened[0]['sha256']}
        operations += [self.job + '-completion-' + str(i) for i in range(len(manifest['completion_steps']))]
        intents = self.events('intent')
        task_intents = [e for e in intents if '-unmarked-recovery-' not in e.get('operation', '')
                        and e.get('action') not in ('recover_expired', 'release')]
        files = manifest.get('files') or []
        permitted = ('request','claim','renew','execute','ack','write_files','import') if files else ('request','claim','renew','execute','ack')
        if any(e.get('action') not in permitted for e in task_intents):
            raise ValueError('File/import or unsupported mutation in Edit-only phase')
        import_operation = None
        if files:
            import_operation = self.job+'-write'
            writes = [e for e in task_intents if e.get('action')=='write_files']
            confirmed = self.events('import_wait_confirmed')
            if (len(writes)!=1 or writes[0].get('operation')!=import_operation
                    or sum(e.get('action')=='import' for e in task_intents)!=1
                    or len(confirmed)!=1 or confirmed[0].get('operation')!=import_operation
                    or confirmed[0].get('ticket')!=row['ticket']):
                raise ValueError('A single verified completed import is required')
        if [e.get('operation') for e in task_intents if e.get('action') == 'execute'] != operations:
            raise ValueError('Task command sequence differs or contains a replay')
        acknowledgements = list(operations)
        if files: acknowledgements.insert(1, import_operation)
        if [e.get('operation') for e in task_intents if e.get('action') == 'ack'] != acknowledgements:
            raise ValueError('Task acknowledgements incomplete or repeated')
        return dict(version=VERSION, ticket=row['ticket'], manifest_digest=row['digest'],
            original_scene=editor['scene'], editor_pid=editor['pid'], terminal_receipt=end,
            managed_scenes=list(dict.fromkeys([editor['scene']]+[manifest[k]['path'] for k in ('scene','away_scene') if k in manifest])),
            import_operation=import_operation,
            saved_scene=saved_scene,
            terminal_value=manifest['completion_steps'][-1]['expect'],
            original_result=json.loads(row['result']), created=time.time())

    def clean(self, state, plan):
        editor = state.get('editor') or {}
        imported = state.get('import')
        import_matches = (isinstance(imported,dict) and imported.get('state')=='complete'
                          and imported.get('operation')==plan['import_operation']) if plan.get('import_operation') else imported is None
        return (editor.get('idle') is True and editor.get('pid') == plan['editor_pid']
                and editor.get('scene') in plan['managed_scenes'] and import_matches
                and all(editor.get(k) is False for k in ('playing','changing','compiling','updating','dirty')))

    def owned(self, state, row):
        holder = state.get('holder') or {}
        return holder.get('ticket') == row['ticket'] and holder.get('owner') == 'coordinator-' + self.job

    def verify_files(self, row, plan):
        if plan['import_operation']:
            self.worker.files_match(json.loads(row['manifest']),after=True)
        saved = plan.get('saved_scene')
        if saved and sha256((self.worker.root/saved['path']).read_bytes()).hexdigest()!=saved['sha256']:
            raise ValueError('Saved scene changed after its verified reopen; no cleanup mutation')

    def run(self):
        row = self.store.get(self.job)
        if row['state'] != 'quarantined' or not row['token'] or not self.matches(row):
            return False
        worker = self.worker
        try:
            proof = self.origin(row)
            plans = self.events('unmarked_cleanup_plan')
            plan = plans[-1] if plans else proof
            if any(plan.get(k) != proof[k] for k in ('version','ticket','manifest_digest','original_scene','editor_pid','terminal_receipt','terminal_value','import_operation','saved_scene','managed_scenes')):
                return self.note('Unmarked cleanup plan identity changed')
            worker.client = worker.factory()
            worker.client.token = row['token']
            state = worker.status(self.job, owner=False)
            release_intents = [e for e in self.events('intent') if e.get('action') == 'release']
            if plans and release_intents and not self.owned(state,row):
                return self.finish(plan, {'owned_holder_absent_after_release_intent': True})
            if not self.owned(state,row):
                return self.note('Unmarked cleanup holder differs; no cross-owner action')
            state = worker.status(self.job, owner=True)
            if not self.owned(state,row) or not self.clean(state,plan):
                return self.note('Unmarked cleanup waits for original clean idle scene and same Editor PID')
            self.verify_files(row,plan)
            holder, receipt = state.get('holder') or {}, state.get('receipt') or {}
            if holder.get('blocked') not in (None,'','owner_deadline_expired'):
                return self.note('Unmarked cleanup unresolved authority block')
            allowed = {plan['terminal_receipt']}
            if plans:
                allowed.update(e['operation'] for e in self.events('intent')
                    if e.get('action') == 'execute' and e.get('operation','').startswith(self.job+'-unmarked-recovery-'))
            if (receipt.get('id') not in allowed or receipt.get('state') not in ('acknowledged','complete')
                    or (receipt.get('result') or {}).get('success') is not True):
                return self.note('Unmarked cleanup needs a known successful terminal receipt')
            if not plans:
                if receipt['state'] != 'acknowledged' or (receipt['result'].get('data') or {}).get('result') != plan['terminal_value']:
                    return self.note('Unmarked cleanup final task check receipt not acknowledged')
                self.store.event(self.job, 'unmarked_cleanup_plan', plan)
            elif receipt['state'] == 'complete':
                worker.require(worker.rpc(self.job, 'ack', operation=receipt['id']))
            if holder.get('blocked') == 'owner_deadline_expired':
                worker.require(worker.rpc(self.job, 'recover_expired'))
            # The inspection is read-only. A completed blocked observation can be
            # replaced by a fresh observation; an uncertain request is never replayed.
            count = sum(e['operation'].startswith(self.job+'-unmarked-recovery-inspect-')
                        for e in self.events('cleanup_recovery_step'))
            probe = self.step('unmarked-recovery-inspect-'+str(count),
                'var s=UnityEditor.SceneManagement.EditorSceneManager.GetActiveScene(); '
                'if(UnityEditor.EditorApplication.isPlayingOrWillChangePlaymode || '
                'UnityEditor.EditorApplication.isCompiling || UnityEditor.EditorApplication.isUpdating || '
                'UnityEngine.SceneManagement.SceneManager.sceneCount!=1 || s.isDirty || System.Array.IndexOf(new string[]{' + ','.join(json.dumps(p) for p in plan['managed_scenes']) + '},s.path)<0' +
                ' || UnityEditor.SessionState.GetString("UnityWorkflow.PlayOwner", "")!="") return "blocked"; '
                'return "unmarked-play-ended-clean-idle";')
            if (probe.get('data') or {}).get('result') != 'unmarked-play-ended-clean-idle':
                return self.note('Unmarked cleanup scene/Play marker changed; no Stop or marker erase')
            if state['editor']['scene'] != plan['original_scene']:
                restored = self.step('unmarked-recovery-restore-scene',
                    'var s=UnityEditor.SceneManagement.EditorSceneManager.GetActiveScene(); '
                    'if(UnityEditor.EditorApplication.isPlayingOrWillChangePlaymode || '
                    'UnityEngine.SceneManagement.SceneManager.sceneCount!=1 || s.isDirty || s.path!=' + json.dumps(state['editor']['scene']) +
                    ' || UnityEditor.SessionState.GetString("UnityWorkflow.PlayOwner", "")!="") throw new System.Exception("Owned cleanup scene changed"); '
                    'UnityEditor.SceneManagement.EditorSceneManager.OpenScene(' + json.dumps(plan['original_scene']) +
                    ',UnityEditor.SceneManagement.OpenSceneMode.Single); return "restored";')
                if (restored.get('data') or {}).get('result') != 'restored':
                    return self.note('Owned original scene restoration not confirmed')
            console = self.step('unmarked-recovery-console', command='read_console',
                parameters={'action':'get','types':['error'],'count':20})
            state = worker.status(self.job, owner=True)
            if not self.owned(state,row) or not self.clean(state,plan) or state['editor']['scene'] != plan['original_scene']:
                return self.note('Unmarked cleanup final owner/idle check failed; release refused')
            self.verify_files(row,plan)
            if release_intents:
                return self.note('Unmarked cleanup prior release unresolved; no replay')
            worker.require(worker.rpc(self.job,'release'))
            return self.finish(plan, {'release_confirmed':True,'console':console,'editor_before_release':state['editor']})
        except Exception as error:
            return self.note(type(error).__name__+': '+str(error)[:300])
        finally:
            if worker.client:
                worker.client.close(); worker.client=None

    def finish(self, plan, evidence):
        self.store.event(self.job,'unmarked_cleanup_recovered',evidence)
        self.worker.finish(self.job,'abandoned',dict(
            reason='External Play ended; service-owned Edit-only cleanup recovered; original task remains unsuccessful',
            original_result=plan['original_result'],cleanup_recovered=True,
            unmarked_cleanup_recovery=VERSION,original_scene=plan['original_scene'],
            editor_before_release=evidence.get('editor_before_release'),console=evidence.get('console'),
            recovery_evidence=evidence),True)
        return True
