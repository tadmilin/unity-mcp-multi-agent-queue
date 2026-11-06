"""Bounded scene/Play phases using the existing FIFO authority and receipt ledger.

The separate phase ledger reuses schema-1 Store/Worker without changing the file
coordinator database, its worker, or frozen sources. No uncertain command replay.
"""
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
import time
import sys
import tempfile

from bridge import Bridge, PROJECT, coordinator
from import_wait import ImportWait
from claim_wait import ClaimWait
from initial_wait import InitialReadinessWait
LOADED_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

sys.path.insert(0, str(PROJECT / 'Tools/UnityJobCoordinator'))
_spec = importlib.util.spec_from_file_location('workflow_phase_worker_base', PROJECT / 'Tools/UnityJobCoordinator/worker.py')
_worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_worker)


class Uncertain(RuntimeError):
    pass


class CheckFailed(RuntimeError):
    pass


def command_failure(inner):
    # Keep compiler lines; "Compilation failed" alone leaves the author guessing.
    detail = str(inner.get('error') or inner.get('message'))
    data = inner.get('data')
    errors = data.get('errors') if isinstance(data, dict) else None
    if isinstance(errors, list) and errors:
        detail += ': ' + ' | '.join(str(error) for error in errors)
    return 'Command failed: ' + detail[:1200]


class PhaseWorker(_worker.Worker):
    def wait_for_claim(self, job):
        return ClaimWait(self, job, self.claim_initial, deadline=self.claim_deadline).run()

    def import_files(self, job, manifest):
        self.files_match(manifest)
        state = self.wait_status(job, owner=True)
        if (state.get('persistenceVersion') != 'fail-closed-v2' or
                state.get('importScheduler') != 'synchronous-refresh-v3'):
            raise CheckFailed('Required file gate version not loaded')
        files = self.wire_files(job, manifest, state)
        operation = job + '-write'
        # Never cleanup/release past an uncertain write or import.
        with ImportWait(self, job, operation, state) as observation:
            from import_recovery import checkpoint, fault
            checkpoint(self, job, observation, state)
            self.safe = False
            data = self.require(self.rpc(job, 'write_files', operation=operation, files=files))
            self.require(data['result'])
            self.require(self.rpc(job, 'ack', operation=operation))
            observation.staged()
            if manifest.get('import_recovery'):
                self.store.event(job, 'import_recovery_staged', {
                    'receipt_hash': observation.receipt_hash, 'requires_reload': observation.reload})
                fault(self, job, 'before_import_intent')
            self.require(self.rpc(job, 'import'))
            if manifest.get('import_recovery'):
                fault(self, job, 'after_import_response')
            observation.wait()
        self.files_match(manifest, after=True)
        self.safe = True

    def scene_precondition(self, target):
        path = self.root / target['path']
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'missing'
        if actual != target['expectedSha256']:
            raise CheckFailed('Requested scene hash changed while queued')
        return path

    def prepare_save_scene(self, job, manifest, original):
        target = manifest['scene']
        if target['path'] == original:
            raise CheckFailed('scene_save target must differ from the original scene')
        path = self.scene_precondition(target)
        self.scene_precondition(manifest['away_scene'])
        backup = self.root / 'Backups/workflow-scene-save' / job
        backup.mkdir(parents=True, exist_ok=False)
        hashes = {}
        for source in (path, Path(str(path) + '.meta')):
            if source.exists():
                data = source.read_bytes()
                (backup / source.name).write_bytes(data)
                hashes[source.name] = hashlib.sha256(data).hexdigest()
        (backup / 'before.json').write_text(json.dumps({'scene': target, 'files': hashes}, indent=2))
        if target['expectedSha256'] == 'missing':
            # Baseline on disk makes the deliberately unsaved control observable.
            self.code(job, 'create-scene',
                'var s=UnityEditor.SceneManagement.EditorSceneManager.NewScene(UnityEditor.SceneManagement.NewSceneSetup.DefaultGameObjects, UnityEditor.SceneManagement.NewSceneMode.Single); '
                'if(!UnityEditor.SceneManagement.EditorSceneManager.SaveScene(s,' + json.dumps(target['path']) + ')) throw new System.Exception("Baseline save failed"); return "baseline-saved";')
        else:
            self.code(job, 'load-scene', 'UnityEditor.SceneManagement.EditorSceneManager.OpenScene('
                + json.dumps(target['path']) + ', UnityEditor.SceneManagement.OpenSceneMode.Single); return "loaded";')

    def reopen_saved_scene(self, job, manifest):
        target, away = manifest['scene']['path'], manifest['away_scene']['path']
        guard = ('if(UnityEngine.SceneManagement.SceneManager.sceneCount!=1 || '
                 'UnityEditor.SceneManagement.EditorSceneManager.GetActiveScene().path!=' + json.dumps(target)
                 + ') throw new System.Exception("Scene ownership mismatch"); ')
        if manifest['save_scene']:
            self.code(job, 'save-scene', guard +
                'if(!UnityEditor.SceneManagement.EditorSceneManager.SaveScene(UnityEditor.SceneManagement.EditorSceneManager.GetActiveScene())) throw new System.Exception("SaveScene failed"); return "saved";')
        self.scene_precondition(manifest['away_scene'])
        # Explicit scene_save contract allows discarding ONLY its target's edits.
        # Opening another scene first proves the check is against a fresh load.
        actual = self.code(job, 'reopen-from-disk', guard +
            'UnityEditor.SceneManagement.EditorSceneManager.OpenScene(' + json.dumps(away) + ', UnityEditor.SceneManagement.OpenSceneMode.Single); '
            'UnityEditor.SceneManagement.EditorSceneManager.OpenScene(' + json.dumps(target) + ', UnityEditor.SceneManagement.OpenSceneMode.Single); '
            'return UnityEditor.SceneManagement.EditorSceneManager.GetActiveScene().isDirty ? "dirty" : "reopened-from-disk";')
        if actual != 'reopened-from-disk':
            raise CheckFailed('Fresh disk scene is not clean')
        self.store.event(job, 'scene_reopened', {'path': target, 'away_scene': away, 'save_requested': manifest['save_scene'],
                                               'sha256': hashlib.sha256((self.root / target).read_bytes()).hexdigest()})

    def recover_quarantined(self, job):
        from unmarked_play_recovery import UnmarkedPlayRecovery
        if UnmarkedPlayRecovery.matches(self.store.get(job)):
            return UnmarkedPlayRecovery(self, job).run()
        manifest = json.loads(self.store.get(job)['manifest'])
        if manifest.get('import_recovery'):
            from import_recovery import eligible_tick
            return eligible_tick(self, job)
        from recovery import CleanupRecovery
        return CleanupRecovery(self, job).run()

    def recover_command(self, job, operation):
        # MCP transport failures may be returned as success=false instead of
        # exceptions. In both cases observe the original operation, never send
        # it again. A pending receipt can still finish while Unity is occupied.
        deadline = time.monotonic() + self.timeout
        while True:
            state = self.wait_status(job, owner=True, deadline=deadline)
            holder, receipt = state.get('holder') or {}, state.get('receipt') or {}
            if holder.get('ticket') != self.store.get(job)['ticket'] or holder.get('blocked'):
                raise Uncertain('Command ownership changed or blocked')
            if receipt.get('id') != operation:
                raise Uncertain('Command receipt does not match; no replay or cleanup')
            if receipt.get('state') == 'complete':
                self.store.event(job, 'terminal_receipt_recovered', {'operation': operation})
                return {'success': True, 'data': {'result': receipt['result']}}
            if receipt.get('state') != 'pending' or time.monotonic() >= deadline:
                raise Uncertain('Command outcome unavailable; no replay or cleanup')
            time.sleep(self.interval)

    def reconcile_ack(self, job, operation, result):
        # ACK is a receipt compare-and-set, never a replay of the workload.
        # One supplemental attempt is permitted only after fresh complete proof.
        deadline = time.monotonic() + self.timeout
        expected_pid = (getattr(self, 'claim_initial', None) or {}).get('editor', {}).get('pid')
        expected_token = self.store.get(job)['token']
        if type(expected_pid) is not int or expected_pid <= 0 or not expected_token:
            raise Uncertain('Acknowledgement reconciliation origin unavailable')
        expected_hash = None
        self.store.event(job, 'ack_reconciliation_started', {'operation': operation})

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise Uncertain('Acknowledgement reconciliation deadline exceeded')
            return value

        def observe():
            nonlocal expected_hash
            while True:
                budget = remaining()
                if self.client.token != expected_token:
                    raise Uncertain('Acknowledgement reconciliation token changed')
                transport = self.client.client
                previous = transport.timeout
                try:
                    transport.timeout = min(previous, budget)
                    state = self.status(job, owner=True)
                except _worker.StatusUnavailable:
                    time.sleep(min(self.interval, remaining()))
                    continue
                finally:
                    transport.timeout = previous
                remaining()
                holder, receipt = state.get('holder') or {}, state.get('receipt') or {}
                receipt_hash = receipt.get('sha256')
                if (self.client.token != expected_token or
                        holder.get('ticket') != self.store.get(job)['ticket'] or
                        holder.get('owner') != 'coordinator-' + job or holder.get('blocked') or
                        holder.get('deadline', 0) <= time.time() or
                        state.get('editor', {}).get('pid') != expected_pid or
                        receipt.get('id') != operation or receipt.get('result') != result or
                        not isinstance(receipt_hash, str) or not receipt_hash or
                        (expected_hash is not None and receipt_hash != expected_hash) or
                        receipt.get('state') not in ('complete', 'acknowledged')):
                    raise Uncertain('Acknowledgement reconciliation evidence mismatch')
                expected_hash = receipt_hash
                return receipt['state']

        if observe() == 'acknowledged':
            self.store.event(job, 'ack_reconciliation_confirmed', {'operation': operation, 'supplemental_ack': False})
            return
        self.store.event(job, 'ack_reconciliation_supplemental', {'operation': operation, 'receipt_hash': expected_hash})
        transport = self.client.client
        previous = transport.timeout
        try:
            transport.timeout = min(previous, remaining())
            self.require(self.rpc(job, 'ack', operation=operation))
        except Exception:
            # A late original ACK may win the CAS; a lost supplemental reply may
            # also have committed. Only observation can establish either result.
            pass
        finally:
            transport.timeout = previous
        if observe() != 'acknowledged':
            raise Uncertain('Acknowledgement still incomplete after one supplemental attempt')
        self.store.event(job, 'ack_reconciliation_confirmed', {'operation': operation, 'supplemental_ack': True})

    def command(self, job, label, code=None, command='execute_code', parameters=None):
        operation = job + '-' + label
        parameters = parameters if parameters is not None else {'action': 'execute', 'code': code}
        self.safe = False
        try:
            response = self.rpc(job, 'execute', operation=operation, command=command, parameters=parameters)
        except Exception:
            response = None
        if not isinstance(response, dict) or response.get('success') is not True:
            response = self.recover_command(job, operation)
        outer = self.require(response)
        inner = outer['result']
        try:
            self.require(self.rpc(job, 'ack', operation=operation))
        except Exception:
            self.reconcile_ack(job, operation, inner)
        self.safe = True
        if inner.get('success') is not True:
            raise CheckFailed(command_failure(inner))
        return inner

    def code(self, job, label, code):
        return self.command(job, label, code=code)['data']['result']

    def wait_editor(self, job, predicate, seconds=150):
        deadline = time.monotonic() + seconds
        while True:
            state = self.wait_status(job, owner=True, deadline=deadline)
            holder = state.get('holder') or {}
            if holder.get('ticket') != self.store.get(job)['ticket'] or holder.get('blocked'):
                self.safe = False
                raise Uncertain('Phase ownership changed or blocked')
            if predicate(state['editor']):
                return state
            if time.monotonic() >= deadline:
                raise CheckFailed('Editor transition deadline exceeded')
            time.sleep(self.interval)

    def check_steps(self, job, stage, steps):
        for index, step in enumerate(steps):
            self.require(self.rpc(job, 'renew'))
            actual = self.code(job, f'{stage}-{index}', step['code'])
            self.store.event(job, 'check', {'stage': stage, 'index': index, 'actual': actual, 'expected': step['expect']})
            if actual != step['expect']:
                raise CheckFailed(f'{stage} check {index}: expected {step["expect"]!r}, got {actual!r}')

    def delay(self, job, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(min(5, max(0, deadline - time.monotonic())))
            self.require(self.rpc(job, 'renew'))

    def process(self, job):
        row = self.store.get(job)
        manifest = json.loads(row['manifest'])
        attempted, claimed, original, play_marked = False, False, None, False
        self.safe = True
        failure = None
        completion_checks = manifest.get('completion_steps', [])
        completion_passed = False
        scene_reopened = False
        try:
            if coordinator.digest(coordinator.encode(manifest).encode()) != row['digest']:
                raise ValueError('Stored phase digest mismatch')
            self.client = self.factory()
            self.timeout = manifest['queue_timeout_seconds']
            self.claim_deadline = time.monotonic() + self.timeout
            self.claim_initial = InitialReadinessWait(self, job, self.claim_deadline).run()
            remaining = self.claim_deadline - time.monotonic()
            if remaining <= 0:
                raise CheckFailed('Queue deadline exceeded before request')
            transport = self.client.client
            old_timeout = transport.timeout
            try:
                transport.timeout = min(old_timeout, remaining)
                attempted = True
                self.require(self.rpc(job, 'request', owner='coordinator-' + job, seconds=1800))
            finally:
                transport.timeout = old_timeout
            if not self.wait_for_claim(job):
                self.client.close()
                self.client = None
                return
            claimed = True
            self.store.update(job, claimed=time.time())
            initial = self.wait_status(job, owner=True)
            original = initial['editor']['scene']
            # Never discard a user's unsaved scene or additive setup.
            if not original or not initial['editor']['idle']:
                raise CheckFailed('Requires an existing saved clean scene')
            self.code(job, 'preflight',
                'if(UnityEngine.SceneManagement.SceneManager.sceneCount!=1) throw new System.Exception("Single scene required"); '
                'return "single-clean-scene";')
            if manifest['kind'] == 'files':
                self.import_files(job, manifest)
            target = manifest.get('scene')
            if manifest['kind'] == 'scene_save':
                self.prepare_save_scene(job, manifest, original)
            elif target:
                self.scene_precondition(target)
                self.code(job, 'load-scene', 'UnityEditor.SceneManagement.EditorSceneManager.OpenScene('
                    + json.dumps(target['path']) + ', UnityEditor.SceneManagement.OpenSceneMode.Single); return "loaded";')
            self.check_steps(job, 'edit', manifest['edit_steps'])
            if manifest['kind'] == 'scene_save':
                self.reopen_saved_scene(job, manifest)
                scene_reopened = True
            if manifest['play']:
                # This marker survives domain reload, and can only be set in our held phase.
                play_marked = True
                self.command(job, 'start-play', command='workflow_play',
                    parameters={'action': 'start', 'job': job})
                self.wait_editor(job, lambda e: e['playing'] and not e['compiling'] and not e['updating'])
                self.check_steps(job, 'play', manifest['play_steps'])
                self.delay(job, manifest['play_seconds'])
                if manifest['pause_seconds']:
                    self.code(job, 'pause', 'UnityEditor.EditorApplication.isPaused=true; return UnityEditor.EditorApplication.isPaused ? "paused" : "failed";')
                    self.delay(job, manifest['pause_seconds'])
                    self.code(job, 'resume', 'UnityEditor.EditorApplication.isPaused=false; return "resumed";')
            # A start acknowledgement is not the result of an asynchronous probe.
            # Check its terminal outcome while our Play is still owned, then use
            # the same cleanup path for both accepted and rejected task results.
            self.check_steps(job, 'completion', completion_checks)
            completion_passed = bool(completion_checks)
        except Exception as error:
            failure = type(error).__name__ + ': ' + str(error)
        try:
            if claimed and self.safe:
                state = self.wait_status(job, owner=True)
                if play_marked:
                    # Atomic marker/ownership check and Stop, not an unowned global Stop.
                    self.command(job, 'stop-owned-play', command='workflow_play',
                        parameters={'action': 'stop', 'job': job})
                    state = self.wait_editor(job, lambda e: not e['playing'] and not e['changing'] and not e['compiling'] and not e['updating'])
                    self.code(job, 'clear-play-owner', 'UnityEditor.SessionState.EraseString("UnityWorkflow.PlayOwner"); return "cleared";')
                elif state['editor']['playing'] or state['editor']['changing']:
                    raise Uncertain('Unmarked Play; refused to stop')
                if original and state['editor']['scene'] != original:
                    if state['editor']['dirty']:
                        if not (manifest['kind'] == 'scene_save' and state['editor']['scene'] == manifest['scene']['path']):
                            raise CheckFailed('Scene dirty; refused to discard changes')
                        self.code(job, 'verify-owned-dirty-scene',
                            'if(UnityEngine.SceneManagement.SceneManager.sceneCount!=1) throw new System.Exception("Single scene required"); return "owned-target-discard-authorized";')
                    self.code(job, 'restore-scene', 'UnityEditor.SceneManagement.EditorSceneManager.OpenScene('
                        + json.dumps(original) + ', UnityEditor.SceneManagement.OpenSceneMode.Single); return "restored";')
                console = self.command(job, 'console', command='read_console',
                                       parameters={'action': 'get', 'types': ['error'], 'count': 20})
                state = self.wait_status(job, owner=True)
                if not state['editor']['idle'] or (original and state['editor']['scene'] != original):
                    raise CheckFailed('Editor not clean/idle in the original scene; release refused')
                self.require(self.rpc(job, 'release'))
                self.finish(job, 'failed' if failure else 'succeeded',
                    {'scope': 'bounded files/scene/Play, declared terminal checks, own cleanup and release',
                     'reason': failure or '', 'console': console, 'original_scene': original,
                     'completion_check_count': len(completion_checks),
                     'completion_checks_passed': completion_passed,
                     'scene_reopened_from_disk': scene_reopened,
                     'scene_save_requested': manifest.get('save_scene'),
                     'editor_before_release': state['editor']}, True)
            elif attempted:
                self.finish(job, 'quarantined', {'reason': failure or 'Ownership uncertain; no replay'})
            else:
                self.finish(job, 'failed', {'reason': failure}, True)
        except Exception as error:
            self.finish(job, 'quarantined', {'reason': (failure or '') + '; cleanup: ' + type(error).__name__ + ': ' + str(error)})
        finally:
            if self.client:
                self.client.close()
                self.client = None


class PhaseBridge(Bridge):
    durable_quarantine_wait = True
    def validate_new_submission(self, manifest):
        if not manifest.get('completion_steps'):
            raise ValueError('New phases require completion_steps; no terminal evidence means unverified')

    def __init__(self, root=PROJECT, **kwargs):
        home = Path(root) / 'JobQueue/phases'
        kwargs.setdefault('worker_check', lambda: home.is_dir() and coordinator.worker_running(home))
        super().__init__(root=root, home=home, **kwargs)

    @staticmethod
    def outcome(snapshot, timed_out=False):
        raw_result = snapshot.get('result') or {}
        original = raw_result.get('original_scene')
        result = Bridge.outcome(snapshot, timed_out)
        editor = result['snapshot']['result'].get('editor_before_release') or {}
        result['schema'] = 'unity-phase-mcp-v1'
        result['recovery'] = 'Use editor_queue workflow_result with mode=phase and the same submission_key, or unity_phase_result; never create a replacement key for an uncertain result'
        result['scope'] = 'declared files/scene/Play terminal checks, owned cleanup, original scene restoration, release receipt and empty error-console'
        result['original_scene'] = original
        result['scene_restored'] = bool(original) and editor.get('scene') == original
        result['play_stopped'] = editor.get('playing') is False and editor.get('changing') is False
        if result['verified_success'] and not (result['scene_restored'] and result['play_stopped']):
            result['verified_success'] = False
            result['reason'] = 'phase_cleanup_not_verified'
        result['completion_check_count'] = raw_result.get('completion_check_count', 0)
        result['completion_checks_passed'] = raw_result.get('completion_checks_passed', False)
        result['scene_reopened_from_disk'] = raw_result.get('scene_reopened_from_disk', False)
        result['scene_save_requested'] = raw_result.get('scene_save_requested')
        result['task_verified_success'] = (
            result['verified_success'] and result['completion_checks_passed'] is True
            if result['completion_check_count'] > 0 else None)
        if raw_result.get('import_recovery_version') and raw_result.get('cleanup_recovered'):
            result['task_verified_success'] = False
        return result

    def prepare(self, manifest_path):
        path = self.inside(manifest_path)
        if path.relative_to(self.root).parts[0].lower() in ('assets', 'packages', 'projectsettings'):
            raise ValueError('Stage phase manifests outside import roots')
        if path.stat().st_size > 256 * 1024:
            raise ValueError('Phase manifest exceeds 256 KiB')
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        allowed = {'kind', 'scene', 'edit_steps', 'play_steps', 'completion_steps', 'play', 'play_seconds', 'pause_seconds', 'queue_timeout_seconds', 'files', 'depends_on', 'away_scene', 'save_scene', 'import_recovery'}
        if not isinstance(raw, dict) or set(raw) - allowed or raw.get('kind') not in ('scene_play', 'scene_save', 'files', 'check'):
            raise ValueError('Expected bounded phase/files/check manifest with known fields')
        kind = raw['kind']
        result = {'kind': kind, 'depends_on': [], 'files': []}
        if kind in ('files', 'check'):
            # Reuse the production file canonicalizer, limits and immutable bytes.
            descriptor = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json', dir=path.parent, delete=False) as stream:
                    json.dump({k: raw[k] for k in ('kind', 'files', 'depends_on') if k in raw}, stream)
                    descriptor = Path(stream.name)
                result.update(Bridge.prepare(self, descriptor.relative_to(self.root).as_posix()))
            finally:
                if descriptor is not None:
                    descriptor.unlink(missing_ok=True)
        elif raw.get('files') or raw.get('depends_on'):
            raise ValueError('Scene phases cannot include file writes/dependencies')
        result['play'] = raw.get('play', kind == 'scene_play')
        if type(result['play']) is not bool:
            raise ValueError('play must be boolean')
        for name, default, low, high in [('play_seconds', 1, 0, 120), ('pause_seconds', 0, 0, 30), ('queue_timeout_seconds', 900, 10, 1700)]:
            value = raw.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ValueError('Invalid ' + name)
            result[name] = value
        for stage in ('edit_steps', 'play_steps', 'completion_steps'):
            # Preserve canonical payloads/digests of existing submission keys.
            if stage == 'completion_steps' and stage not in raw:
                continue
            steps = raw.get(stage, [])
            if not isinstance(steps, list) or len(steps) > 16:
                raise ValueError('Maximum 16 checks per stage')
            result[stage] = []
            for step in steps:
                if (not isinstance(step, dict) or set(step) != {'code_file', 'expect'}
                    or not isinstance(step['code_file'], str) or not isinstance(step['expect'], str)):
                    raise ValueError('Each check needs code_file and a string expect')
                source = self.inside((path.parent / step['code_file']).relative_to(self.root).as_posix())
                if source.relative_to(self.root).parts[0].lower() in ('assets', 'packages', 'projectsettings'):
                    raise ValueError('Stage check code outside import roots')
                if source.stat().st_size > 32768:
                    raise ValueError('Check code exceeds 32 KiB')
                result[stage].append({'code': source.read_text(encoding='utf-8-sig'), 'expect': step['expect']})
        if not result['play'] and (result['play_steps'] or result['pause_seconds']):
            raise ValueError('Play checks/pause require play=true')
        if kind != 'scene_play' and not result.get('completion_steps'):
            raise ValueError('New file/check/save tasks require nonempty completion_steps')
        if kind == 'scene_save':
            if result['play'] or not raw.get('scene') or not raw.get('away_scene'):
                raise ValueError('scene_save requires play=false, scene and away_scene')
            result['save_scene'] = raw.get('save_scene', True)
            if type(result['save_scene']) is not bool:
                raise ValueError('save_scene must be boolean')
        elif 'save_scene' in raw or 'away_scene' in raw:
            raise ValueError('Save fields require kind=scene_save')
        for name in ('scene', 'away_scene'):
            if not raw.get(name):
                continue
            scene = raw[name]
            if not isinstance(scene, dict) or set(scene) != {'path', 'expectedSha256'}:
                raise ValueError('Scene requires path and expectedSha256')
            scene_path = self.inside(scene['path'])
            if scene_path.relative_to(self.root).parts[0] != 'Assets' or scene_path.suffix != '.unity':
                raise ValueError('Scene must be an existing Assets/*.unity file')
            coordinator.target_path(self.root, scene['path'])
            actual = hashlib.sha256(scene_path.read_bytes()).hexdigest() if scene_path.is_file() else 'missing'
            if actual == 'missing' and not (kind == 'scene_save' and name == 'scene' and scene_path.parent.is_dir()):
                raise ValueError('Only scene_save may create a scene in an existing directory')
            if actual != scene['expectedSha256']:
                raise ValueError('Scene hash mismatch')
            result[name] = scene
        if kind == 'scene_save' and result['scene']['path'] == result['away_scene']['path']:
            raise ValueError('away_scene must differ from target')
        if 'import_recovery' in raw:
            from import_recovery import validate_contract
            result['import_recovery'] = validate_contract(result, raw['import_recovery'])
        return result


class PhaseService:
    """One service-owned executor; observation cancellation never strands a ticket."""
    def __init__(self, root=PROJECT):
        self.root = Path(root)
        self.bridge = PhaseBridge(root)
        self.thread = None
        self.lock = threading.Lock()
        self.error = None

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            home = self.bridge.home
            home.mkdir(parents=True, exist_ok=True)
            if coordinator.worker_running(home):
                return
            with contextlib.closing(coordinator.Store(home)) as store:
                store.control('stop', '0')
            (home / 'installed.json').write_text('{"scope":"scene_play/scene_save/checked-files"}')
            ready = threading.Event()
            def run():
                try:
                    with coordinator.WorkerLock(home), contextlib.closing(coordinator.Store(home)) as store:
                        store.interrupted()
                        ready.set()
                        worker = PhaseWorker(store, self.root, interval=1)
                        next_recovery = 0
                        while store.control('stop') != '1':
                            if time.monotonic() >= next_recovery:
                                for row in store.db.execute("SELECT id FROM jobs WHERE state='quarantined' ORDER BY created").fetchall():
                                    worker.recover_quarantined(row['id'])
                                next_recovery = time.monotonic() + 5
                            # Keep admitted jobs durable but do not claim/execute
                            # across either executor's unresolved quarantine.
                            job = None if self.bridge.quarantine_blockers() else store.next_job()
                            if job:
                                worker.process(job)
                            else:
                                time.sleep(.25)
                except Exception as error:
                    self.error = str(error)
                    ready.set()
            self.thread = threading.Thread(target=run, name='unity-phase-executor', daemon=True)
            self.thread.start()
            if not ready.wait(5) or self.error:
                raise RuntimeError(self.error or 'Phase worker startup deadline')

    def close(self):
        if self.thread and self.thread.is_alive():
            with contextlib.closing(coordinator.Store(self.bridge.home)) as store:
                store.control('stop', '1')
            self.thread.join(timeout=180)
