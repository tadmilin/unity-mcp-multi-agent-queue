"""Workspace-local MCP service. Existing Unity MCP/worker processes stay intact."""
import argparse
import json
import os
from pathlib import Path
import site
import sqlite3
import sys
from hashlib import sha256

ROOT = Path(__file__).resolve().parents[2]
# Same 68 hash-pinned wheels, reinstalled after the old runtime became unreadable.
RUNTIME = ROOT / '.tools/unity-workflow-runtime-20261027'
sys.path.insert(0, str(RUNTIME))
site.addsitedir(str(RUNTIME))
os.environ.setdefault('FASTMCP_CHECK_FOR_UPDATES', 'off')
os.environ.setdefault('DO_NOT_TRACK', '1')

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from bridge import Bridge
from import_wait import VERSION as IMPORT_WAIT_VERSION, LOADED_SHA256 as IMPORT_WAIT_SHA256
from claim_wait import VERSION as CLAIM_WAIT_VERSION, LOADED_SHA256 as CLAIM_WAIT_SHA256
from initial_wait import VERSION as INITIAL_WAIT_VERSION, LOADED_SHA256 as INITIAL_WAIT_SHA256
import importlib.util
_lifetime_spec = importlib.util.spec_from_file_location('workflow_process_lifetime', ROOT / 'Tools/UnityNativeObserver/process_watch.py')
_lifetime_module = importlib.util.module_from_spec(_lifetime_spec)
_lifetime_spec.loader.exec_module(_lifetime_module)
SERVICE_STARTED_TICKS = _lifetime_module.process_started_ticks()
SERVER_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()


def import_wait_capabilities():
    from unmarked_play_recovery import VERSION as UNMARKED_VERSION, LOADED_SHA256 as UNMARKED_SHA256
    from phase import LOADED_SHA256 as PHASE_SHA256
    from import_recovery import VERSION as RECOVERY_VERSION, LOADED_SHA256 as RECOVERY_SHA256
    return {'unmarked_play_cleanup_recovery': UNMARKED_VERSION, 'unmarked_play_recovery_sha256_at_load': UNMARKED_SHA256, 'import_cleanup_recovery': RECOVERY_VERSION, 'import_recovery_sha256_at_load': RECOVERY_SHA256, 'checked_import_wait': IMPORT_WAIT_VERSION, 'import_wait_sha256_at_load': IMPORT_WAIT_SHA256,
            'preclaim_wait': CLAIM_WAIT_VERSION, 'claim_wait_sha256_at_load': CLAIM_WAIT_SHA256,
            'initial_readiness_wait': INITIAL_WAIT_VERSION, 'initial_wait_sha256_at_load': INITIAL_WAIT_SHA256,
            'phase_sha256_at_load': PHASE_SHA256, 'server_sha256_at_load': SERVER_SHA256,
            'dependency_runtime': str(RUNTIME)}


def create_server(bridge=None):
    bridge = bridge or Bridge()
    server = FastMCP('unity-workflow', instructions=(
        'Use unity_job_run once with a stable submission key and a staged manifest. '
        'The service waits for the coordinator, compilation and release internally. '
        'Do not write shell polling or repeat status calls during normal waiting. '
        'After a lost response, reuse the same key or recover with unity_job_result. '
        'Observation timeout/cancellation does not cancel the durable job. '
        'Never treat coordinator success with console errors as verified success.'))

    def response(value):
        return ToolResult(content=json.dumps(value), structured_content=value,
                          is_error=value.get('verified_success') is False)

    @server.tool
    async def unity_job_run(submission_key: str, manifest_path: str, wait_seconds: int = 600) -> ToolResult:
        """Submit staged files/check once and return after completion; waiting stays inside this MCP call.

        Paths are relative to this fixed workspace. Release any token you hold
        before submitting. Use 0..1800 seconds; configure the client timeout above
        that budget. No worker/Unity restart, automatic recovery or Play control.
        """
        try:
            return response(await bridge.run(submission_key, manifest_path, wait_seconds))
        except (ValueError, OSError, sqlite3.Error) as error:
            return response({'verified_success': False, 'reason': str(error),
                             'submission_key': submission_key, 'recovery': 'Query the same key if acceptance is uncertain'})

    @server.tool
    async def unity_job_result(job_id: str | None = None, submission_key: str | None = None,
                               wait_seconds: int = 600) -> ToolResult:
        """Recover an existing durable job by ID/key and wait internally. Does not submit or retry mutations."""
        import asyncio
        try:
            snapshot = await asyncio.to_thread(bridge.snapshot, job_id, submission_key)
            return response(await bridge.wait(snapshot['id'], wait_seconds))
        except (ValueError, OSError, sqlite3.Error) as error:
            return response({'verified_success': False, 'reason': str(error), 'submission_key': submission_key})

    @server.tool
    def unity_workflow_capabilities() -> dict:
        """Report the fixed workspace, supported operations and observer limitations."""
        return {**bridge.capabilities(), 'service_pid': os.getpid(), 'service_started_ticks': SERVICE_STARTED_TICKS, **import_wait_capabilities()}

    # Native clients can keep their already-connected Unity server. The Editor
    # ingress delegates workflow_* actions here; all gate actions remain intact.
    from phase import PhaseService
    from starlette.responses import JSONResponse
    import asyncio
    phases = PhaseService(bridge.root)
    server.phase_service = phases
    from job_router import JobRouter
    bridge = JobRouter(bridge, phases.bridge)

    @server.tool
    async def unity_phase_run(submission_key: str, manifest_path: str, wait_seconds: int = 600) -> ToolResult:
        """Run a bounded scene/Play phase, including FIFO wait, owned cleanup and release."""
        try:
            await asyncio.to_thread(phases.start)
            return response(await phases.bridge.run(submission_key, manifest_path, wait_seconds))
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
            return response({'verified_success': False, 'reason': str(error), 'submission_key': submission_key})

    @server.tool
    async def unity_phase_result(submission_key: str, wait_seconds: int = 600) -> ToolResult:
        """Recover an existing phase by stable key; never submit or replay mutations."""
        try:
            snapshot = await asyncio.to_thread(phases.bridge.snapshot, None, submission_key)
            return response(await phases.bridge.wait(snapshot['id'], wait_seconds))
        except (ValueError, OSError, sqlite3.Error) as error:
            return response({'verified_success': False, 'reason': str(error), 'submission_key': submission_key})

    @server.custom_route('/workflow', methods=['POST'])
    async def native_workflow(request):
        try:
            if request.headers.get('origin'):
                return JSONResponse({'success': False, 'error': 'Browser origins are not allowed'}, status_code=403)
            body = await request.body()
            if len(body) > 32768:
                raise ValueError('Native request exceeds 32 KiB')
            p = json.loads(body)
            if not isinstance(p, dict):
                raise ValueError('Native request must be an object')
            action = p.get('action')
            if action == 'workflow_capabilities':
                return JSONResponse({'success': True, 'data': {
                    **bridge.capabilities(), 'service_pid': os.getpid(), 'service_started_ticks': SERVICE_STARTED_TICKS, **import_wait_capabilities(), 'native_ingress': 'editor_queue.workflow_run',
                    'phase_kinds': ['scene_play', 'scene_save'], 'phase_worker_running': phases.bridge.worker_check(),
                    'closed_turn_resume': 'not_qualified', 'automatic_uncertain_replay': False}})
            if p.get('mode') not in ('job', 'phase'):
                raise ValueError('mode must be job or phase')
            selected = phases.bridge if p['mode'] == 'phase' else bridge
            key = p.get('submission_key')
            if action == 'workflow_run':
                if p['mode'] == 'phase':
                    await asyncio.to_thread(phases.start)
                job = await asyncio.to_thread(selected.submit, key, p['manifest_path'])
            elif action in ('workflow_result', 'workflow_poll'):
                job = (await asyncio.to_thread(selected.snapshot, None, key))['id']
            else:
                raise ValueError('Unknown workflow action')
            value = await selected.wait(job, 0)
            return JSONResponse({'success': True, 'data': value})
        except (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error) as error:
            return JSONResponse({'success': False, 'error': str(error)})

    return server


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport', choices=['stdio', 'http'], default='stdio')
    parser.add_argument('--port', type=int, default=8091)
    args = parser.parse_args()
    server = create_server()
    # Resume eligible cleanup even when no client starts another phase.
    server.phase_service.start()
    if args.transport == 'http':
        try:
            server.run(transport='http', host='127.0.0.1', port=args.port, show_banner=False)
        finally:
            server.phase_service.close()
    else:
        server.run(transport='stdio', show_banner=False)
