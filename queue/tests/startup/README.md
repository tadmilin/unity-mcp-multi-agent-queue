# Workflow startup regression

Requires Python and the .NET 8 SDK. Run from the repository root:

```powershell
python queue/tests/startup/run.py
```

This compiles the **actual exported `EditorQueueWorkflow.cs`** through a project link. Global aliases replace HTTP and process-start boundaries; small stubs supply Unity/package response APIs. Requests run through the real private `Workflow` method. No sockets are opened and no service is started.

Cases cover a missing service (including capability registration), a service already running, a foreign workspace, a missing installed interpreter, and a missing server script. Temporary fixtures include spaces in their paths. The pre-fix source fails `offline`: it selects `C:/Python313/python.exe` instead of the fixture's installer-created environment. The missing-file cases also fail before the fix.

To additionally check a real, existing installer fixture:

```powershell
python queue/tests/startup/run.py --installed-project "C:\UnityProjects\QueueTest"
```

That extra case captures the real startup arguments, appends `--help`, and runs the selected interpreter and script. The script exits before creating the service. It does not alter the fixture or initialize a job. It is not a live Unity test.

Use `--output path/to/new-report.json` to save a summary. The runner refuses to overwrite reports. Build outputs are ignored. This harness does not verify Unity compilation, the real HTTP registration, real service startup or reload timing; those still need an isolated Unity acceptance run.
