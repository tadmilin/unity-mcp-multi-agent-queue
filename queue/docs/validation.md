# Export validation and known limits

## Scoped recovery qualification

The current candidate passed 109 focused phase/import/recovery tests and eight installer tests. The first isolated Unity build exposed an invalid 33-character `WorkflowPlay.cs.meta` GUID: Unity exited successfully while ignoring that script. The GUID is corrected to 32 characters, installer regression coverage checks script GUIDs, and a later live registration and owned Play prove the actual tool loaded.

An initial batch Editor attempt was rejected by the existing process-identity guard before requesting a ticket. The operator switched to a normal Editor; the guard was not changed. A basic check then passed. The first Preview exposed a lost start-Play ACK and was quarantined. Evidence was preserved before operator cleanup and the existing resolution route marked it abandoned, never successful.

The bounded ACK fix permits one supplemental receipt acknowledgement only after matching authenticated complete evidence, then requires observed acknowledged state. The next accepted Preview encountered the same natural ACK-disconnect path, recovered in 10.371 seconds from the failed ACK response, passed its in-Play completion check, stopped its owned Play, restored the scene and released the gate. It issued one start and one stop. A separate real C# write/import/domain reload passed with one write and one import; it did not exercise unavailable import confirmation.

This is a scoped qualification on one machine, not a speed/token comparison, clean-machine certification or proof of every recovery route. See [recovery details](recovery.md) and [sanitized evidence](recovery-live-validation.json). Services were started explicitly; automatic C# fallback startup remains separately scoped by its boundary tests.

## Workflow startup interpreter fix

The exported Unity ingress selected `C:/Python313/python.exe` when the workflow endpoint was unavailable, although the installer creates `.unity-mcp-queue/venv/Scripts/python.exe`. A standalone .NET 8 harness compiles the actual ingress source and invokes its real `Workflow` method, replacing only HTTP/process launch boundaries and Unity/package API surfaces. The pre-fix `offline` case deterministically failed twice with `expected installer venv, got C:/Python313/python.exe`; the already-online and wrong-workspace controls passed. The installed fixture had both the expected interpreter and server, ruling out incomplete installation as the explanation for the wrong selection. The missing-interpreter/server cases also showed attempted launches before the fix.

The fallback now selects the installer environment relative to the project root, checks that the interpreter and script exist, and reports explicit missing-file errors. It keeps quoted script arguments, a project working directory, hidden process startup, workspace validation and same-key recovery guidance. It does not add another interpreter fallback or change endpoint configuration.

Validation: all six C# boundary cases pass. A seventh case used the selected real interpreter/arguments on the existing installed fixture with `--help` appended; it passed without creating a service listener. All seven installer tests were rerun and passed. This does **not** verify live Unity compilation, real network startup, reload behavior or job execution. The earlier 255 Python/3 JavaScript suite results below are historical and were not rerun for this C#-only fix.

See [startup tests](../tests/startup/README.md) and `startup-python-test-results.json`. The source manifest retains the original input hash and updates the exported C# hash. The production game project's copy was not changed. Earlier installation/CLI-help tests missed this path because they started from an explicitly selected Python and did not exercise C# fallback selection.

## Installer follow-up

The root README now describes this queue extension; the pinned upstream README is preserved separately as `README.upstream.md`. `queue/install.py` adds first-install setup and a no-write `--check` for a closed, isolated Windows Unity project. It preserves other manifest fields, refuses existing installations/locks, records partial failures, and does not start Unity or services.

Dependency diagnosis: the first fresh environment successfully installed the original workflow lock and passed `pip check`, but the combined import check failed at `fastapi`. Interpreter-prefix/module inspection confirmed the correct new environment and absent `fastapi`/`tomli`; native metadata declares both, and native telemetry directly imports `tomli`. The dependency-contract regression test additionally exposed workflow MCP 1.26.0 versus native metadata MCP 1.30.0. Thus a successful workflow-only installation did not qualify the native environment.

`queue/requirements-install-lock.txt` is a separate combined lock: the original workflow pins are retained except MCP is 1.30.0, with FastAPI 0.162.0, tomli 2.5.0 and FastAPI's annotated-doc 0.0.5 added. Hashes come from downloaded wheels. The original exported workflow lock remains unchanged for historical reproducibility. The regression test was observed failing before this correction and passing afterward.

Seven installer tests passed, including real coordinator initialization in a temporary project-shaped fixture with environment provisioning substituted. A separate fresh environment then completed the real network install with required hashes and no pip cache, `pip check`, import checks, overlay/package copying and coordinator initialization. Both installed server entry points exited successfully for `--help`; no server or Editor was started.

The entire previous offline suite was rerun with that new combined environment: **255 Python tests passed, 2 experimental tests skipped; all 3 JavaScript suites passed**. These are separate from the 7 installer tests. See `offline-test-results-installer.json` and `installer-validation.json`. The fixture has Unity-like directories but was not created/opened by Unity. C# compilation, package resolution, live MCP connection, reload and Play acceptance remain unverified.

This branch is a research source preview based on a pinned upstream commit. The original game project and running services were not modified while preparing it.

## Export changes

- Preserved the six package changes: two modified upstream C# files and two added gate classes with their metadata.
- Selected the workflow/coordinator runtime, existing offline tests, maintained native-server snapshot and required Editor integration.
- Excluded original deployment/recovery one-off scripts, game content, runtime directories, raw AI logs and private local configuration.
- Replaced two hardcoded observer workspace paths with the owning Unity project's path derived from the module location.
- Added `observe_reload.py`, a transitive import needed by the shared response decoder; it was missing from the first export selection.
- Updated two old transport test fixtures to provision both ledgers and seed an existing legacy job. New unchecked jobs are intentionally rejected by current `JobRouter`; a separate added SDK test verifies rejection without admission. No production admission rule was relaxed.
- Marked two tests for the unenabled experimental EditorWait-to-PhaseWorker integration as skipped with an explicit reason. Their candidate module/unit tests remain available; this integration is not a claimed feature.
- Added a runner that prioritizes the selected dependency directory and excludes user-site packages when that directory is supplied.

## Debugging ledger

1. Initial export: coordinator 38 tests with one missing-import error; workflow 206 tests with four errors; native observer 12 tests passed; three JavaScript suites passed.
2. Re-ran the four workflow failures with the pinned dependency directory ahead of other packages and user-site disabled: all four still reproduced. Dependency ordering alone did not explain them.
3. Source tracing identified the missing transitive import, two obsolete fixture assumptions, an unprovisioned second ledger, and two candidate integration expectations absent from the active phase implementation.
4. Corrected the export/fixtures, retained the disabled integration as explicit skips, and added rejection coverage. The three affected SDK/HTTP tests passed in a focused rerun.
5. The final complete offline report is `offline-test-results-final.json`. Read its counts and skips; an exit-code pass does not include skipped tests as successes.

Final result: **255 Python tests passed, 2 experimental integration tests skipped**, and all **3 JavaScript suites passed**. The dependency directory was the existing pinned environment, not a fresh network installation. Copied native-server sources retain upstream whitespace; whitespace checking for new work excludes that preserved snapshot and checks the two relocated observer modules separately.

Export digests in `source-manifest.json` normalize text line endings to LF so a Git checkout on another platform does not invalidate provenance merely because of CRLF conversion. Original source digests are raw bytes.

## What is not validated

- The initial export had no live Unity acceptance. The scoped recovery qualification above now covers its listed cases; broad visual/asset and multi-agent performance acceptance remains unfinished.
- Fresh network Python installation passes in the installer fixture described above. Clean-machine Unity installation remains unqualified.
- Offline fake-authority/temporary-ledger tests do not prove live Editor behavior.
- Historical 24-success/1-abandoned job outcomes do not certify all content or unattended recovery.
- Complete cross-layer network retry totals and causal call-reduction/speed claims remain unestablished.

Initial and intermediate test failures are retained in sanitized public summaries and in the private source-workspace handoff; they are not reported as successful runs.
