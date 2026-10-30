"""Opt-in, receipt-bound import cleanup. Never resume user code or replay a mutation."""
import base64
import hashlib
import json
from pathlib import Path
import re
import time

from import_wait import ImportWait, boot_id
from initial_wait import live_unity_process

VERSION = 'owned-import-cleanup-v1'
LOADED_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def validate_contract(manifest, contract):
    if (manifest['kind'] != 'files' or manifest.get('play') or manifest.get('scene') or
            manifest.get('edit_steps') or manifest.get('play_steps') or
            not isinstance(contract, list) or not 1 <= len(contract) <= 16):
        raise ValueError('Import recovery requires files-only and 1..16 declarative checks')
    files = {f['path']: f for f in manifest['files']}
    covered = set()
    for check in contract:
        if not isinstance(check, dict):
            raise ValueError('Invalid recovery check')
        kind, path = check.get('kind'), check.get('path')
        item = files.get(path)
        if not item or item.get('delete') or path in covered:
            raise ValueError('Recovery checks must cover unique nondeleted manifest files')
        if kind == 'text_asset':
            if set(check) != {'kind', 'path'} or not path.endswith('.txt'):
                raise ValueError('text_asset recovery supports .txt only')
        elif kind == 'script_constant':
            if set(check) != {'kind', 'path', 'type', 'field', 'value'} or not path.endswith('.cs'):
                raise ValueError('Invalid script_constant recovery check')
            if (not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', check.get('type', '')) or
                    not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', check.get('field', '')) or
                    not isinstance(check.get('value'), str) or not 1 <= len(check['value']) <= 256):
                raise ValueError('Invalid constant identity/value')
        else:
            raise ValueError('Unsupported import recovery oracle')
        covered.add(path)
    # Meta files are checked by after-hashes; all other files need a Unity oracle.
    if any(f.get('delete') or (f['path'] not in covered and
            not (f['path'].endswith('.meta') and f['path'][:-5] in covered)) for f in manifest['files']):
        raise ValueError('Every recovery file must have an allowlisted Unity oracle')
    return contract


def oracle_code(manifest):
    lines = ['// Import recovery: generated read-only checks, no user completion code.']
    for i, c in enumerate(manifest['import_recovery']):
        path = json.dumps(c['path'])
        if c['kind'] == 'text_asset':
            item = next(f for f in manifest['files'] if f['path'] == c['path'])
            expected = hashlib.sha256(base64.b64decode(item['base64'])).hexdigest()
            lines += [f'var a{i}=UnityEditor.AssetDatabase.LoadAssetAtPath<UnityEngine.TextAsset>({path});',
                f'if(a{i}==null) throw new System.Exception("Recovery asset missing");',
                f'using(var h{i}=System.Security.Cryptography.SHA256.Create()) '
                f'if(System.BitConverter.ToString(h{i}.ComputeHash(a{i}.bytes)).Replace("-", "").ToLowerInvariant()!={json.dumps(expected)}) '
                'throw new System.Exception("Recovery loaded bytes mismatch");']
        else:
            lines += [f'var s{i}=UnityEditor.AssetDatabase.LoadAssetAtPath<UnityEditor.MonoScript>({path});',
                f'var t{i}=s{i}==null ? null : s{i}.GetClass();',
                f'if(t{i}==null || t{i}.FullName!={json.dumps(c["type"])}) throw new System.Exception("Recovery loaded type mismatch");',
                f'var f{i}=t{i}.GetField({json.dumps(c["field"])},System.Reflection.BindingFlags.Public|System.Reflection.BindingFlags.Static|System.Reflection.BindingFlags.DeclaredOnly);',
                f'if(f{i}==null || !f{i}.IsLiteral || f{i}.FieldType!=typeof(string) || '
                f'(string)f{i}.GetRawConstantValue()!={json.dumps(c["value"])}) throw new System.Exception("Recovery compiled constant mismatch");']
    return '\n'.join(lines + ['return "import-recovery-loaded";'])


def fault(worker, job, point):
    """Explicit local test fixture, exact key+digest; absent by default, consumed once."""
    path = worker.root / 'Library/ImportRecoveryFaults.json'
    if not path.is_file():
        return
    value = json.loads(path.read_text(encoding='utf-8'))
    row = worker.store.get(job)
    if value != {'version': 1, 'submission_key': row['submit_key'], 'digest': row['digest'], 'point': point}:
        return
    if worker.store.db.execute("SELECT 1 FROM events WHERE job=? AND kind='import_recovery_fault'", (job,)).fetchone():
        return
    worker.store.event(job, 'import_recovery_fault', {'point': point, 'synthetic_transport_loss': True})
    raise RuntimeError('Owned import recovery fault: ' + point)


def checkpoint(worker, job, observation, initial):
    if not json.loads(worker.store.get(job)['manifest']).get('import_recovery'):
        return
    worker.store.event(job, 'import_recovery_origin', {
        'version': VERSION, 'digest': worker.store.get(job)['digest'], 'initial': initial,
        'connection': observation.connection, 'created_ns': time.time_ns()})


class ImportCleanupRecovery:
    def __init__(self, worker, job):
        self.worker, self.store, self.job = worker, worker.store, job

    def events(self, kind):
        return [json.loads(r[0]) for r in self.store.db.execute(
            'SELECT detail FROM events WHERE job=? AND kind=? ORDER BY seq', (self.job, kind))]

    def note(self, error):
        reason = str(error)[:500]
        prior = self.events('import_recovery_wait')
        if not prior or prior[-1]['reason'] != reason:
            self.store.event(self.job, 'import_recovery_wait', {'reason': reason})
        return False

    def owned(self, state, row, origin):
        h, e = state.get('holder') or {}, state.get('editor') or {}
        if (h.get('ticket') != row['ticket'] or h.get('owner') != 'coordinator-' + self.job or
                h.get('blocked') or h.get('deadline', 0) <= time.time() or
                e.get('pid') != origin['initial']['editor']['pid'] or
                e.get('playing') or e.get('changing') or e.get('dirty') or
                e.get('scene') != origin['initial']['editor']['scene']):
            raise ValueError('Recovery holder/lease/Editor/scene mismatch')

    def step(self, label, code=None, command='execute_code', parameters=None):
        operation = self.job + '-' + label
        done = [e for e in self.events('import_recovery_step') if e['operation'] == operation]
        if done:
            return done[-1]['result']
        intents = [e for e in self.events('intent') if e.get('operation') == operation and e['action'] == 'execute']
        if intents:
            if len(intents) != 1:
                raise ValueError('Duplicate recovery execute intent')
            state = self.worker.status(self.job, owner=True)
            self.owned(state, self.row, self.origin)
            receipt = state.get('receipt') or {}
            if receipt.get('id') != operation or receipt.get('state') not in ('complete', 'acknowledged'):
                raise ValueError('Recovery execute outcome unknown; no replay')
            result = receipt.get('result') or {}
            if result.get('success') is not True:
                raise ValueError('Recovery execute failed; no replay')
            if receipt['state'] == 'complete':
                if any(e.get('action') == 'ack' and e.get('operation') == operation for e in self.events('intent')):
                    raise ValueError('Recovery ack outcome unknown; no replay')
                self.worker.require(self.worker.rpc(self.job, 'ack', operation=operation))
        else:
            # A generated read-only command is still a receipt-bearing gate mutation.
            parameters = parameters if parameters is not None else {'action': 'execute', 'code': code}
            response = self.worker.require(self.worker.rpc(self.job, 'execute',
                operation=operation, command=command, parameters=parameters))
            result = response['result']
            if result.get('success') is not True:
                raise ValueError('Recovery command failed; no replay')
            self.worker.require(self.worker.rpc(self.job, 'ack', operation=operation))
        self.store.event(self.job, 'import_recovery_step', {'operation': operation, 'result': result})
        return result

    def finish(self, plan):
        ready = self.events('import_recovery_ready')[-1]
        self.store.event(self.job, 'import_cleanup_recovered', {'version': VERSION, 'ticket': self.row['ticket']})
        self.worker.finish(self.job, 'abandoned', {
            'reason': 'Import cleanup recovered; original task remains unsuccessful',
            'original_result': plan['original_result'], 'cleanup_recovered': True,
            'task_verified_success': False, 'completion_checks_passed': False,
            'console': ready['console'], 'editor_before_release': ready['editor'],
            'original_scene': self.origin['initial']['editor']['scene'],
            'import_recovery_version': VERSION}, True)
        return True

    def run(self):
        w, job = self.worker, self.job
        row = self.row = self.store.get(job)
        if row['state'] != 'quarantined' or not row['token']:
            return False
        manifest = json.loads(row['manifest'])
        if not manifest.get('import_recovery'):
            return False
        try:
            from bridge import coordinator
            validate_contract(manifest, manifest['import_recovery'])
            if coordinator.digest(coordinator.encode(manifest).encode()) != row['digest']:
                raise ValueError('Recovery manifest digest mismatch')
            origins = self.events('import_recovery_origin')
            staged = self.events('import_recovery_staged')
            if (len(origins) != 1 or len(staged) != 1 or origins[0]['digest'] != row['digest'] or
                    origins[0].get('version') != VERSION):
                raise ValueError('Complete origin/staged checkpoint unavailable')
            origin = self.origin = origins[0]
            if not live_unity_process(origin['initial']['editor']['pid'], origin['created_ns'], w.root):
                raise ValueError('Original Editor process/lifetime unavailable')
            plans = self.events('import_recovery_plan')
            plan = plans[-1] if plans else {'digest': row['digest'], 'ticket': row['ticket'],
                'original_result': json.loads(row['result'] or '{}')}
            if plan['digest'] != row['digest'] or plan['ticket'] != row['ticket']:
                raise ValueError('Recovery plan identity changed')
            intents = self.events('intent')
            releases = [e for e in intents if e.get('action') == 'release']
            if releases:
                replies = [e for e in self.events('response') if e['action'] == 'release']
                if (len(releases) == 1 and len(replies) == 1 and replies[0]['response'].get('success') is True
                        and self.events('import_recovery_ready')):
                    return self.finish(plan)
                raise ValueError('Release outcome unresolved; no replay')
            w.client = w.factory()
            w.client.token = row['token']
            state = w.status(job, owner=True)
            self.owned(state, row, origin)
            w.files_match(manifest, after=True)
            if not plans:
                self.store.event(job, 'import_recovery_plan', plan)
            confirmed = self.events('import_recovery_confirmed')
            if not confirmed:
                writes = [e for e in intents if e.get('action') == 'write_files']
                acks = [e for e in intents if e.get('action') == 'ack' and e.get('operation') == job + '-write']
                imports = [e for e in intents if e.get('action') == 'import']
                receipt, current = state.get('receipt') or {}, state.get('import') or {}
                if (len(writes) != 1 or writes[0].get('operation') != job + '-write' or len(acks) != 1 or
                        len(imports) > 1 or receipt.get('id') != job + '-write' or
                        receipt.get('sha256') != staged[0]['receipt_hash'] or receipt.get('state') != 'acknowledged' or
                        (receipt.get('result') or {}).get('success') is not True or
                        current.get('operation') != job + '-write' or
                        boot_id(current.get('originBootUtc')) != boot_id(origin['initial']['bootUtc'])):
                    raise ValueError('Write/import journal or receipt identity mismatch')
                if current.get('state') == 'staged' and imports:
                    raise ValueError('Import intent exists but still staged; dispatch unknown, no replay')
                if not imports and current.get('state') != 'staged':
                    raise ValueError('Import state advanced without a durable intent')
                if current.get('state') not in ('staged', 'scheduled', 'running', 'settling', 'reloading', 'complete'):
                    raise ValueError('Unsupported import state; no reconciliation')
                # A listener is armed before the snapshot/first dispatch. A complete
                # import is confirmed directly; no reconnect event is required again.
                w.timeout = manifest['queue_timeout_seconds']
                with ImportWait(w, job, job + '-write', origin['initial']) as observation:
                    for field in ('native_pid', 'epoch'):
                        if observation.connection[field] != origin['connection'][field]:
                            raise ValueError('Original native process changed')
                    observation.connection = origin['connection']
                    observation.receipt_hash = staged[0]['receipt_hash']
                    observation.reload = staged[0]['requires_reload']
                    observation.deadline = min(time.monotonic() + w.timeout,
                        time.monotonic() + state['holder']['deadline'] - time.time())
                    observation.snapshot()
                    if not imports:
                        w.require(w.rpc(job, 'import'))
                    completed = observation.confirm() if current['state'] == 'complete' else observation.wait()
                    self.owned(completed, row, origin)
                    if not observation.reload and boot_id(completed['bootUtc']) != boot_id(origin['initial']['bootUtc']):
                        raise ValueError('Unexpected domain reload for text-only import')
                    self.store.event(job, 'import_recovery_confirmed', {'state': completed})
            else:
                # Checks replace the current receipt; retain the original import proof.
                if not state['editor'].get('idle'):
                    raise ValueError('Editor not idle after proven import')
                if boot_id(state['bootUtc']) != boot_id(confirmed[-1]['state']['bootUtc']):
                    raise ValueError('Editor boot changed after import confirmation')
                receipt = state.get('receipt') or {}
                allowed = [job + suffix for suffix in ('-write', '-import-recovery-loaded', '-import-recovery-console')]
                if (receipt.get('id') not in allowed or receipt.get('state') not in ('complete', 'acknowledged') or
                        (receipt.get('result') or {}).get('success') is not True):
                    raise ValueError('Unexpected receipt after import confirmation')
            loaded = self.step('import-recovery-loaded', oracle_code(manifest))
            if (loaded.get('data') or {}).get('result') != 'import-recovery-loaded':
                raise ValueError('Loaded-state oracle failed')
            console = self.step('import-recovery-console', command='read_console',
                                parameters={'action': 'get', 'types': ['error'], 'count': 20})
            # The shared Console can contain earlier jobs' errors. Retain them;
            # complete import + loaded oracle + clean idle prove safe cleanup,
            # never task success. A failed compile cannot reach this branch.
            if console.get('success') is not True or not isinstance(console.get('data'), list):
                raise ValueError('Console evidence unavailable; import cleanup refused')
            state = w.status(job, owner=True)
            self.owned(state, row, origin)
            current = state.get('import') or {}
            if (not state['editor'].get('idle') or current.get('state') != 'complete' or
                    current.get('operation') != job + '-write'):
                raise ValueError('Final Editor/import cleanup not idle')
            w.files_match(manifest, after=True)
            self.store.event(job, 'import_recovery_ready', {'editor': state['editor'], 'console': console})
            w.require(w.rpc(job, 'release'))
            fault(w, job, 'after_release_response')
            return self.finish(plan)
        except Exception as error:
            return self.note(type(error).__name__ + ': ' + str(error))
        finally:
            if w.client:
                w.client.close()
                w.client = None


def eligible_tick(worker, job):
    """No periodic Unity RPC: rescan only after a local authority/native change."""
    stamps = []
    for name in ('Library/EditorQueueGate/state.json', 'Library/UnityWorkflowSignals/native-connected.json'):
        try:
            info = (worker.root / name).stat()
            stamps.append((info.st_mtime_ns, info.st_size))
        except OSError:
            stamps.append(None)
    seen = getattr(worker, '_import_recovery_seen', {})
    signature = tuple(stamps)
    if seen.get(job) == signature:
        return False
    # Save before running: if our recovery changes authority state, one further
    # pass can recover a lost checkpoint. Stable unresolved state sends no RPC.
    seen[job] = signature
    worker._import_recovery_seen = seen
    return ImportCleanupRecovery(worker, job).run()
