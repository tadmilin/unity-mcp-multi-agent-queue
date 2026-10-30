"""Project-specific native ingress; submission and observation stay outside Unity.

The central service retains all admission, FIFO, execution and cleanup authority.
Each native call issues one workload/result RPC. It never retries a submission.
"""
import asyncio
from datetime import datetime, timezone
from hashlib import sha1, sha256
import json
from code_fingerprint import fingerprint, VERSION as FINGERPRINT_VERSION
from process_watch import guarded_workload
import os
from pathlib import Path
import time

from fastmcp import Client
from models.models import MCPResponse

VERSION = "native-outside-unity-v2-service-lifetime"
WORKSPACE = Path(__file__).resolve().parents[2]
PROJECT_ID = sha1((WORKSPACE / "Assets").as_posix().encode()).hexdigest()[:16]
ENDPOINT = "http://127.0.0.1:8091/mcp"
TRACE = WORKSPACE / "docs/validation/unity-native-observer-20261027/native-trace.jsonl"
_ACTIVE = 0
_ROOT = Path(__file__).resolve().parent
_LOADED_FILES = {name: sha256((_ROOT / name).read_bytes()).hexdigest() for name in (
    "main.py", "services/custom_tool_service.py", "native_workflow_observer.py",
    "transport/plugin_hub.py", "import_notifications.py", "code_fingerprint.py", "process_watch.py")}


def handles(project_id, tool_name, params, remote_hosted=False):
    return (not remote_hosted and str(project_id).lower() == PROJECT_ID
            and tool_name == "editor_queue" and isinstance(params, dict)
            and params.get("action") in ("workflow_run", "workflow_result", "workflow_poll"))


def _trace(event, **fields):
    TRACE.parent.mkdir(parents=True, exist_ok=True)
    with TRACE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(),
                                 "pid": os.getpid(), "event": event, **fields}) + "\n")


async def execute(params, max_wait_seconds=1800):
    global _ACTIVE
    key, mode, action = params.get("submission_key"), params.get("mode"), params.get("action")
    if mode not in ("job", "phase") or not isinstance(key, str) or not key.strip():
        return MCPResponse(success=False, error="mode job/phase and nonempty submission_key are required")
    if action == "workflow_run" and not isinstance(params.get("manifest_path"), str):
        return MCPResponse(success=False, error="manifest_path is required for workflow_run")
    wait_seconds = min(1800, max(1, int(max_wait_seconds or 1800)))
    operation = "run" if action == "workflow_run" else "result"
    tool = f"unity_{mode}_{operation}"
    arguments = {"submission_key": key, "wait_seconds": wait_seconds}
    if operation == "run":
        arguments["manifest_path"] = params["manifest_path"]
    started = time.monotonic()
    _ACTIVE += 1
    try:
        _trace("native_enter", key=key, mode=mode, action=action)
        async with asyncio.timeout(wait_seconds + 45):
            async with Client(ENDPOINT, timeout=wait_seconds + 30) as client:
                _trace("capabilities_rpc", key=key, mode=mode)
                capabilities = (await client.call_tool("unity_workflow_capabilities", {})).structured_content
                if (not isinstance(capabilities, dict)
                        or os.path.normcase(os.path.abspath(capabilities.get("workspace", "")))
                        != os.path.normcase(str(WORKSPACE))
                        or capabilities.get("schema") != "unity-workflow-mcp-v1"):
                    raise ValueError("Central service workspace/schema mismatch; no workload sent")
                _trace("workload_rpc", key=key, mode=mode, tool=tool)
                # Exactly one run/result request. No Unity transport and no replay.
                result = await guarded_workload(client, tool, arguments, capabilities)
                value = result.structured_content
                if not isinstance(value, dict):
                    raise ValueError("Central result has no structured outcome; recover the same key")
                if value.get("submission_key") != key:
                    raise ValueError("Central result identity missing/mismatched; recover the same key")
                if not value.get("job_id"):
                    if value.get("verified_success") is not False or not isinstance(value.get("reason"), str):
                        raise ValueError("Central result identity missing/mismatched; recover the same key")
                    # Central refused this key without a job (bad manifest, unknown key):
                    # surface its reason instead of hiding it behind an identity error.
                    _trace("native_rejected", key=key, mode=mode, elapsed_seconds=time.monotonic() - started)
                    return MCPResponse(success=False, error=value["reason"], data=value)
                _trace("native_return", key=key, mode=mode, job_id=value["job_id"],
                       state=value.get("state"), verified_success=value.get("verified_success"),
                       task_verified_success=value.get("task_verified_success"),
                       elapsed_seconds=time.monotonic() - started)
                return MCPResponse(success=value.get("verified_success") is True,
                                   message="Central workflow outcome; native observer stayed outside Unity.",
                                   data=value)
    except asyncio.CancelledError:
        _trace("native_cancelled", key=key, mode=mode, elapsed_seconds=time.monotonic() - started)
        raise
    except Exception as error:
        _trace("native_error", key=key, mode=mode, error_type=type(error).__name__)
        return MCPResponse(success=False,
                           error=f"NATIVE_WORKFLOW_OBSERVATION_FAILED: {error}. Recover using workflow_result and the same mode/key; do not replay.",
                           data={"submission_key": key, "mode": mode,
                                 "job_cancelled_by_observer": False})
    finally:
        _ACTIVE -= 1


def runtime_provenance():
    return {"version": VERSION, "pid": os.getpid(), "workspace": str(WORKSPACE),
            "project_id": PROJECT_ID, "source_root": str(_ROOT),
            "source_sha256_at_load": _LOADED_FILES,
            "execute_code_sha256": fingerprint(execute.__code__),
            "execute_code_fingerprint_version": FINGERPRINT_VERSION,
            "active_observations": _ACTIVE, "unity_observer_polls": 0}
