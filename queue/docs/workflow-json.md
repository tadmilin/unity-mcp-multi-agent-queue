# JSON evidence in workflow code

Use `UnityWorkflow.WorkflowJson.Serialize(value)` when a workflow writes a DTO containing `Vector3` or `Bounds`. The helper writes vectors as numeric `x`, `y`, `z` fields and bounds as `center` and `size`. It supports these values inside ordinary objects, arrays and dictionaries. It does not serialize a whole scene or a Unity object graph.

```csharp
var evidence = new {
    position = transform.position,
    localBounds = renderer.localBounds
};
var text = UnityWorkflow.WorkflowJson.Serialize(evidence);
System.IO.File.WriteAllText(evidencePath, text);
```

The writer must choose an existing, authorized evidence directory. The helper only returns JSON text. It does not create directories, write files, submit jobs or change global `JsonConvert.DefaultSettings`.

Three failed jobs in the recorded character trial called `JsonConvert.SerializeObject` directly on Unity value types. Their errors originated inside the submitted C# code. The default serializer follows computed properties such as `Vector3.normalized` and reports a self-reference. Changing the scheduler or the outer MCP response serializer does not repair those calls.

This helper is **opt-in**. Update the evidence-writing call in new workflow code to use it. Existing manifests are not rewritten or replayed. Ordinary reference cycles still raise an error instead of silently dropping fields.

## Small check after installation

First complete the [installation and connection steps](installation.md), including a saved clean scene and the expected connected project. Copy both files from [`examples/json-check`](../examples/json-check) into the project's `Incoming/QueueJsonCheck` directory. Keep staged manifests and snippet files outside `Assets`, `Packages` and `ProjectSettings`.

Call the native MCP entry point once with a new submission key:

```json
{
  "tool_name": "editor_queue",
  "parameters": {
    "action": "workflow_run",
    "mode": "phase",
    "submission_key": "replace-with-a-new-unique-check-key",
    "manifest_path": "Incoming/QueueJsonCheck/manifest.json",
    "wait_seconds": 120
  }
}
```

These are the arguments to `execute_custom_tool`. If the response is pending or lost, query `workflow_result` with `mode: "phase"` and the **same** submission key. Do not send `workflow_run` again or create a replacement key for an uncertain result. Inspect `task_verified_success`, the terminal state and failure details. A finished request alone is not proof that the check passed.

## Validation scope

The three observed failure shapes were reproduced with the deployed Unity/Newtonsoft value types. Explicit numeric projection retained every tested coordinate. The actual helper then passed 12 checks under .NET 8 and in an isolated Unity Editor batch project. Those checks include three expected baseline failures, the helper outcomes, nested bounds, ordinary values, nulls, decimal culture, reference-cycle rejection and global-settings isolation.

This validates the helper's serialization behavior. It does not establish a multi-agent speedup, a lower token bill or a successful replay of the original character jobs. See [the regression harness](../tests/json/README.md) and [validation receipt](workflow-json-validation.json).
