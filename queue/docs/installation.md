# Preview installation

This first-install command targets a **closed, isolated Windows Unity project**. It does not upgrade an existing queue or replace a stock MCP installation. Windows CPython 3.13 is required by the recorded wheel hashes. Use a test project created by Unity, not the source overlay directory.

## Command

From a checkout of `queue-research-preview`:

```powershell
py -3.13 .\queue\install.py --project "C:\UnityProjects\QueueTest" --check
py -3.13 .\queue\install.py --project "C:\UnityProjects\QueueTest"
```

`--check` validates project structure, lock and destination conflicts only. It does not prove network availability, dependencies, C# compilation or runtime behavior. Keep the target Editor closed throughout installation; a lock check is not an OS-level prohibition on opening Unity.

Setup creates:

- `.unity-mcp-queue/venv`: a new environment at its final location. Downloads require the recorded hashes; there is no fallback to unpinned versions.
- `Packages/com.coplaydev.unity-mcp`: this fork's modified Unity package.
- `Tools/Unity*` and the two `Assets/Editor` integrations from the overlay.
- `JobQueue`: an initialized database and coordinator launchers, with no worker started.
- `.unity-mcp-queue/manifest.before.json`: the original package manifest, backed up before changes.
- `.unity-mcp-queue/install-result.json`: status and paths written. The package reference is committed last.

Other manifest fields and dependencies are preserved. Existing destinations, reparse points, a Unity lock, or an existing MCP dependency cause refusal. No runtime database or service state is copied from the research machine. Exclude `.unity-mcp-queue/`, `JobQueue/`, local logs and credentials from your project's Git history; this installer does not rewrite your `.gitignore`.

## After installation

Installation success is **not Unity acceptance**. Current integration uses ports 8080 and 8091; do not start these alongside another instance using those endpoints. The installer does not stop or replace existing services.

From the target project, start the maintained native server in one terminal:

```powershell
.\.unity-mcp-queue\venv\Scripts\python.exe -s .\Tools\UnityNativeObserver\main.py --transport http --http-url http://127.0.0.1:8080
```

Start the workflow service in another terminal:

```powershell
.\.unity-mcp-queue\venv\Scripts\python.exe -s .\Tools\UnityWorkflowMcp\server.py --transport http --port 8091
```

Open the target project in Unity, let import and compilation finish, then connect its MCP window to the maintained server at `http://127.0.0.1:8080`. Avoid an upstream auto-start flow that selects an unmodified server instead. Configure your MCP client for the corresponding `/mcp` endpoint. Follow the [workflow protocol](../README.md#protocol): verify actual project identity and capabilities, submit one bounded check with a stable key, and inspect terminal evidence. Scene/Play acceptance follows separately.

These startup instructions describe the intended integration; this installer change has not yet been accepted in a live Unity Editor. It does not configure each AI client or guarantee that an ended agent turn resumes.

## Interrupted or failed setup

The command exits nonzero and retains `.unity-mcp-queue/install-result.json` when setup fails after reservation. A dependency failure leaves import roots and the manifest untouched. A later copy/coordinator failure can leave a partial installation; inspect `written_paths` and the manifest backup. A retry refuses existing state instead of silently overwriting it.

For a disposable test project, create a fresh test project to retry after addressing the cause. Do not remove an active queue database or live Unity lock, or restore a manifest over unrelated later edits. This preview has no automatic upgrade, uninstall or rollback.

## Installer verification

```powershell
py -3.13 -m unittest discover -s queue -p test_install.py -v
```

The suite checks conflict/lock refusal, no-write preflight, dependency failure, concurrent manifest changes, and real overlay/coordinator initialization in temporary project-shaped fixtures. Environment provisioning is substituted in the file-integration test. A fixture is not a Unity-generated or live-Editor acceptance test. See [validation](validation.md) for the separate fresh-download result.

The layout follows Unity's [local package paths](https://docs.unity.com/en-us/engine/6000.3/manual/packages-list/dependencies-lp/upm-localpath). Python [virtual environments are not movable](https://docs.python.org/3/library/venv.html), so setup creates one at its final location.
