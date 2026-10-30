# Unity MCP Multi-Agent Queue

A research preview for coordinating multiple AI agents that share one Unity Editor. Built on [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp), with Editor ownership, durable queued jobs, bounded scene/Play workflows, and recorded execution results.

**Preview branch: `queue-research-preview`.** This is an independent extension, not an official CoplayDev release. Installation and live Unity acceptance have separate validation statuses; see [validation](queue/docs/validation.md).

## What this adds

- A shared Editor gate so participating clients execute under the current owner's permission.
- Durable submissions with a stable key and result recovery after a lost response.
- A central executor for waiting, claiming, cleanup and release within supported job/phase scopes.
- Client observation of an existing request without submitting the mutation again.

The queue does not guarantee that an AI session stays alive or resumes after its turn ends. Historical trials do not establish faster execution or fewer total calls. Read the [evaluation and limitations](queue/docs/evaluation.md).

## Install

You need **Windows, Python 3.13 (64-bit), Git, and a Unity project**. Try this on a test project or a copy of your game first. File and Python setup have been tested; setup inside a real Unity Editor is still untested.

### Get the files

Open PowerShell and run:

```powershell
git clone --branch queue-research-preview https://github.com/tadmilin/unity-mcp-multi-agent-queue.git
cd unity-mcp-multi-agent-queue
```

If you already cloned this repo, open PowerShell in that folder instead. Pick the case below that matches your project.

### 1. New project, or no MCP installed

1. Create a project in Unity Hub, or use an existing project with no MCP or queue files.
2. Wait for Unity to finish opening it, then close that project's Unity window. Unity Hub can stay open. Stopping Play mode is not enough.
3. Run this from the repo folder. Replace the path with your Unity project's folder:

```powershell
py -3.13 .\queue\install.py --project "C:\UnityProjects\QueueTest"
```

**You do not need to add CoplayDev's GitHub link in Package Manager first.** The command installs our modified MCP package and the queue tools together. It also updates `Packages/manifest.json` for you. The package still appears as **MCP for Unity**.

### 2. Already using CoplayDev MCP

This is a manual switch for the standard CoplayDev package installed through a Git URL. It has not been tested end to end in Unity yet. There is no automatic upgrade command.

1. Back up your project and MCP client settings. Use a copy of the project for this first try. Keep the old package URL/version so you can restore it.
2. Stop AI clients from sending work to that project. In the copy's Unity Editor, open **Package Manager**, select **In Project**, find **MCP for Unity**, and click **Remove**. Wait for removal to finish.
3. Close the copy's Unity window.
4. From the repo folder, run the same installer, pointing it at the copy:

```powershell
py -3.13 .\queue\install.py --project "C:\UnityProjects\MyGameTestCopy"
```

Do not change the old Git URL to our URL and then run the installer: it will still see an installed MCP package and stop. Removing the old package first lets the installer add our package and queue files together. Unity explains what [Remove does](https://docs.unity.com/en-us/engine/7000.0/manual/packages-list/managing-packages-window/upm-ui-actions/upm-ui-remove).

If you use a locally edited/embedded MCP package, already have our queue, or cannot click Remove, keep the existing files. This guide does not cover those cases. The installer refuses to overwrite them.

### Start the services, then open Unity

Both cases use these steps after installation. If setup fails, read the error and [recovery notes](queue/docs/installation.md#interrupted-or-failed-setup) before continuing.

The services use ports **8080 and 8091**. If another project is already using them, do not start a second pair or stop someone else's services. This installer does not set up separate ports.

Open a new PowerShell window **in your Unity project folder**, not the downloaded repo. Start the MCP server:

```powershell
.\.unity-mcp-queue\venv\Scripts\python.exe -s .\Tools\UnityNativeObserver\main.py --transport http --http-url http://127.0.0.1:8080
```

Leave that window open. Open a second PowerShell window in the same Unity project folder and start the queue service:

```powershell
.\.unity-mcp-queue\venv\Scripts\python.exe -s .\Tools\UnityWorkflowMcp\server.py --transport http --port 8091
```

Now open the project in Unity and wait for import and compilation to finish. In Unity's MCP window, connect to `http://127.0.0.1:8080`. Use the server you just started; an upstream auto-start setup may launch a different server. For the Unity/native tools, set your AI client's HTTP MCP address to `http://127.0.0.1:8080/mcp`. The exact settings depend on the app you use.

Check the connected project and workflow capabilities before sending a small test job. See the [workflow steps](queue/README.md#protocol). Installing files does not prove that Unity compilation, connection or job execution works.

### Check before installing

Add `--check` to either install command to check the folder and look for conflicts without changing files or downloading anything:

```powershell
py -3.13 .\queue\install.py --project "C:\UnityProjects\QueueTest" --check
```

This check rejects a project that still has MCP installed. It does not test Unity or the Python dependencies. See [setup details and recovery](queue/docs/installation.md) for more.

## Documentation

- [Architecture and workflow protocol](queue/README.md)
- [Installer and service startup](queue/docs/installation.md)
- [Observed evaluation](queue/docs/evaluation.md)
- [Tests, skipped experiments and known limits](queue/docs/validation.md)
- [Upstream README, preserved separately](README.upstream.md)

## Attribution and license

This fork retains CoplayDev's source history and MIT notices. The preview was based on upstream commit `aa5fc638d623f56178d50329d9ad8c541a57fe66`. Upstream code and our queue additions are distinguished in the [source layout and provenance](queue/README.md#provenance-and-license); copied upstream code is not presented as a new contribution.

See [LICENSE](LICENSE), [server license](Server/LICENSE), and [maintained-server license](queue/UnityProject/Tools/UnityNativeObserver/LICENSE). The Unity package ID remains `com.coplaydev.unity-mcp` for compatibility.
