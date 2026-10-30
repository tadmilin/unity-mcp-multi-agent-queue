# Export validation and known limits

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

- No new live Unity, import, domain reload, scene, Play or visual acceptance was performed for this checkout.
- No fresh network dependency installation or clean-machine Unity installation has been qualified.
- Offline fake-authority/temporary-ledger tests do not prove live Editor behavior.
- Historical 24-success/1-abandoned job outcomes do not certify all content or unattended recovery.
- Complete cross-layer network retry totals and causal call-reduction/speed claims remain unestablished.

Initial and intermediate test failures are retained in sanitized public summaries and in the private source-workspace handoff; they are not reported as successful runs.
