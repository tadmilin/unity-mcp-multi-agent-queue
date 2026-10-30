# Unity MCP Multi-Agent Queue — research preview

A coordination extension for multiple AI agents sharing one Unity Editor. This branch preserves the CoplayDev source history and adds an Editor gate, durable jobs, bounded scene/Play phases, and a client that can observe a pending job without resubmitting it.

**Status:** source preview. Offline tests and historical local experiments are documented separately. A fresh Unity installation of this exported layout has **not** been validated. Windows-specific notifications and process identity checks are currently required. This is not an official CoplayDev release.

## Source layout

| Path | Purpose |
| --- | --- |
| `../MCPForUnity/Editor/Tools/EditorQueue*` | Editor ownership, receipts, staged imports and cleanup checks |
| `../MCPForUnity/Editor/Tools/CommandRegistry.cs` | Gates command dispatch across clients |
| `../MCPForUnity/Editor/Services/Transport/TransportCommandDispatcher.cs` | Transport integration |
| `UnityProject/Tools/UnityWorkflowMcp` | Durable workflow service and direct JavaScript client |
| `UnityProject/Tools/UnityJobCoordinator` | SQLite jobs, coordinator, worker and local installation helper |
| `UnityProject/Tools/UnityNativeObserver` | Maintained upstream server snapshot with workflow observation and reload notification support |
| `UnityProject/Tools/UnityEditorGate` | Gate client |
| `UnityProject/Tools/UnityReloadDiagnostics` | Shared MCP response/client helpers |
| `UnityProject/Assets/Editor` | Unity-side workflow ingress and readiness publisher |

`UnityProject` is an overlay template, not a complete Unity game project. The Python paths intentionally retain their relative `Tools/...` layout. The two machine-specific observer roots were changed to derive from that layout in this export.

## Protocol

1. Prepare a manifest outside Unity import roots, with a terminal completion check.
2. Submit once using a stable key. The central service queues and claims the Editor.
3. Execute the bounded phase under ownership. Record results before acknowledging operations.
4. Verify the task result and clean up only the owned state before release.
5. Recover a lost response using the original key. Observation cancellation does not cancel the durable job.

`direct_client.mjs` exposes `startWorkflow(...)` and `observeWorkflow(handle, ...)`. Observing an existing handle waits on its existing Promise; it does not send another submission RPC. This does not mean the executor performs no status checks, renewals or cleanup calls.

For a successful task, inspect `task_verified_success` and the task-specific evidence. A transport success, outer receipt success, or saved prefab alone is insufficient. Failed/abandoned jobs remain failed/abandoned. Automatic uncertain mutation replay and waking an ended AI turn are not qualified.

## Integration outline — not clean-install acceptance

Use an isolated Windows Unity project for first integration. Keep one selected project per service instance; local endpoints are presently fixed around ports 8080/8091.

1. Add this branch's `MCPForUnity` directory as a local Unity package. It includes changes to upstream command dispatch, so an unmodified upstream package is not equivalent.
2. Copy the contents of `UnityProject/Tools` to the target project's `Tools`, and `UnityProject/Assets/Editor` to its `Assets/Editor`. Do this during an exclusive maintenance interval with no agent jobs running.
3. Prepare Windows CPython 3.13 dependencies in a virtual environment. `Tools/UnityWorkflowMcp/requirements-lock.txt` records the wheel hashes used in the original environment. A new dependency download/install has not been tested for this export.
4. Run `Tools/UnityJobCoordinator/install.py` from the target project using that interpreter. It initializes local queue files; it does not start Unity.
5. Configure/start the maintained native server from `Tools/UnityNativeObserver/main.py` on loopback 8080 and connect the Unity package to it. Start `Tools/UnityWorkflowMcp/server.py --transport http --port 8091` using the same environment. These are persistent services, not repeated client polling commands.
6. Verify the actual project identity, capabilities, idle state and a scoped check job before attempting scene/Play work.

The dependency environment must include the pinned FastMCP/MCP and Windows process-notification dependencies. The maintained server snapshot's `pyproject.toml` records its dependencies and entry points. Keep the snapshot in the target project's `Tools` layout; copying an old machine's `.tools`, `Library`, queue database, token files or logs is not an installation procedure.

## Offline validation

From the repository root, using an interpreter with the required dependencies:

```powershell
python queue/run_offline_tests.py --output queue/docs/local-test-results.json
```

An optional `--dependency-path` points at an existing dependency directory; the runner gives it precedence and disables user-site packages. Reports refuse overwrite. Tests use temporary ledgers, fake authorities, in-process HTTP transports and test-owned process lifetime checks. They do not call a live Unity Editor.

See [validation and packaging changes](docs/validation.md). Two experimental EditorWait-to-PhaseWorker integration tests are explicitly skipped because that integration is not enabled in this runtime snapshot; they are not counted as passes.

## Evaluation and limitations

[Observed evaluation](docs/evaluation.md) separates agent calls, local observations, shell invocations, executor gate calls and actual task milestones. Different workloads and observation durations prevent a causal speed or total-call-reduction claim. Worker-assisted reload/compile recovery occurred in the recorded run.

No game models, vendor assets, extracted game data, raw conversations, credentials or runtime journals are included in this extension export. Published measurements are aggregate historical observations, not new Unity acceptance results.

## Provenance and license

Based on [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp), commit `aa5fc638d623f56178d50329d9ad8c541a57fe66`. The maintained Python snapshot originated from `mcpforunityserver10.3.0`; its local version label is preserved. The original MIT license and copyright notices are retained at the repository root, under `Server`, and with the maintained snapshot.

`source-manifest.json` records original and exported file hashes. `native-upstream-provenance.json` records the snapshot's upstream hashes without the original machine's absolute cache path. Changes made for packaging are listed in [validation.md](docs/validation.md).
