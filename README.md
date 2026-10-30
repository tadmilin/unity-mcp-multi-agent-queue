# Unity MCP Multi-Agent Queue

A research preview for coordinating multiple AI agents that share one Unity Editor. Built on [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp), with Editor ownership, durable queued jobs, bounded scene/Play workflows, and recorded execution results.

**Preview branch: `queue-research-preview`.** This is an independent extension, not an official CoplayDev release. Installation and live Unity acceptance have separate validation statuses; see [validation](queue/docs/validation.md).

## What this adds

- A shared Editor gate so participating clients execute under the current owner's permission.
- Durable submissions with a stable key and result recovery after a lost response.
- A central executor for waiting, claiming, cleanup and release within supported job/phase scopes.
- Client observation of an existing request without submitting the mutation again.

The queue does not guarantee that an AI session stays alive or resumes after its turn ends. Historical trials do not establish faster execution or fewer total calls. Read the [evaluation and limitations](queue/docs/evaluation.md).

## Install the preview

Use **Windows, CPython 3.13, Git, and a fresh isolated Unity project**. Close that project's Editor before setup. This installer supports first installation only; it refuses existing queue files or an existing MCP for Unity dependency.

```powershell
git clone --branch queue-research-preview https://github.com/tadmilin/unity-mcp-multi-agent-queue.git
cd unity-mcp-multi-agent-queue
py -3.13 .\queue\install.py --project "C:\UnityProjects\QueueTest"
```

Replace the example path with your test project. To check paths and conflicts first, without writes or downloads:

```powershell
py -3.13 .\queue\install.py --project "C:\UnityProjects\QueueTest" --check
```

The command creates a private Python environment, installs hash-pinned dependencies, copies the modified Unity package and project tools, initializes the queue, and backs up/updates the Unity package manifest. **It does not start services or Unity.** A successful installation is reported as `installed_pending_unity_validation`.

[Installation, startup and failure recovery](queue/docs/installation.md) explains the remaining steps. Use an isolated test project for the first trial.

## Documentation

- [Architecture and workflow protocol](queue/README.md)
- [Installer and service startup](queue/docs/installation.md)
- [Observed evaluation](queue/docs/evaluation.md)
- [Tests, skipped experiments and known limits](queue/docs/validation.md)
- [Upstream README, preserved separately](README.upstream.md)

## Attribution and license

This fork retains CoplayDev's source history and MIT notices. The preview was based on upstream commit `aa5fc638d623f56178d50329d9ad8c541a57fe66`. Upstream code and our queue additions are distinguished in the [source layout and provenance](queue/README.md#provenance-and-license); copied upstream code is not presented as a new contribution.

See [LICENSE](LICENSE), [server license](Server/LICENSE), and [maintained-server license](queue/UnityProject/Tools/UnityNativeObserver/LICENSE). The Unity package ID remains `com.coplaydev.unity-mcp` for compatibility.
