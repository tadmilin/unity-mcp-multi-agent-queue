# Workflow JSON regression

Run the actual linked `WorkflowJson.cs` with locally installed Unity and Newtonsoft assemblies. Neither assembly is redistributed in this repository.

```powershell
dotnet run --project queue/tests/json/JsonProbe.csproj `
  --property:UnityManaged="C:\Program Files\Unity\Hub\Editor\YOUR_VERSION\Editor\Data\Managed" `
  --property:UnityNewtonsoft="C:\YourProject\Library\PackageCache\YOUR_NEWTONSOFT_PACKAGE\Runtime\Newtonsoft.Json.dll"
```

The baseline cases must reproduce the recorded `Vector3` self-reference error. The helper must preserve numeric coordinates, nested bounds and ordinary DTO fields without changing global defaults or hiding unrelated reference cycles. A successful exit means all these expectations held, including the deliberately failing baseline cases.

This harness runs outside the Editor under .NET 8. The separate live acceptance receipt documents the same cases executed in an isolated Unity Editor. Neither test submits a queue job or measures orchestration performance.
