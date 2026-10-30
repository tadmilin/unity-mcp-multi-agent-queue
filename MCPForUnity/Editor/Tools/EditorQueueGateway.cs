using System;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using MCPForUnity.Editor.Helpers;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace MCPForUnity.Editor.Tools
{
    [InitializeOnLoad]
    [McpForUnityTool("editor_queue", Description = "Exclusive Editor phase queue. Request/claim a capability token, execute commands, acknowledge receipts, then release after idle. Stage file edits and commit with write_files before import. Direct tools require this gateway.")]
    public static class EditorQueueGateway
    {
        sealed class Permit { public string Token; public string Operation; public bool Active; }
        static readonly AsyncLocal<Permit> Scope = new AsyncLocal<Permit>();
        static EditorQueueState state;
        static bool refreshHeld;
        static bool compilationHadErrors;
        static readonly DateTime BootUtc = DateTime.UtcNow;
        static DateTime loadedStateWriteUtc;
        static string loadedImportOperation;
        static string loadedImportState;
        static Exception persistenceFault;
        static bool importRunning;
        static readonly string Root = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
        static readonly string StatePath = Path.Combine(Root, "Library", "EditorQueueGate", "state.json");

        static EditorQueueGateway()
        {
            AssemblyReloadEvents.beforeAssemblyReload += BeforeReload;
            EditorApplication.quitting += DropRefreshHold;
            EditorApplication.delayCall += AfterReload;
            UnityEditor.Compilation.CompilationPipeline.compilationStarted += _ => compilationHadErrors = false;
            UnityEditor.Compilation.CompilationPipeline.assemblyCompilationFinished += (_, messages) =>
                compilationHadErrors |= messages.Any(message => message.type == UnityEditor.Compilation.CompilerMessageType.Error);
            UnityEditor.Compilation.CompilationPipeline.compilationFinished += _ =>
            {
                if (compilationHadErrors && State.Data["import"] is JObject import)
                { import["state"] = "compile_failed"; Save(State.Data); }
            };
        }

        static EditorQueueState State
        {
            get
            {
                if (persistenceFault != null)
                    throw new InvalidOperationException("EDITOR_QUEUE_PERSISTENCE_FAILED_RELOAD_REQUIRED", persistenceFault);
                if (state != null) return state;
                loadedStateWriteUtc = File.Exists(StatePath) ? File.GetLastWriteTimeUtc(StatePath) : DateTime.MinValue;
                JObject data = null;
                if (File.Exists(StatePath))
                {
                    using var file = new FileStream(StatePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                    using var reader = new StreamReader(file);
                    data = JObject.Parse(reader.ReadToEnd());
                }
                loadedImportOperation = (string)data?["import"]?["operation"];
                loadedImportState = (string)data?["import"]?["state"];
                return state = new EditorQueueState(data, Save,
                    () => (DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds);
            }
        }

        static void Save(JObject value)
        {
            var temp = StatePath + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                if (persistenceFault != null)
                    throw new InvalidOperationException("EDITOR_QUEUE_PERSISTENCE_FAILED_RELOAD_REQUIRED", persistenceFault);
                Directory.CreateDirectory(Path.GetDirectoryName(StatePath));
                using (var file = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                using (var writer = new StreamWriter(file))
                { writer.Write(value.ToString()); writer.Flush(); file.Flush(true); }
                // Retry only the identical disk commit, never a Unity command or transaction.
                for (int attempt = 0; ; attempt++)
                {
                    try
                    {
                        if (File.Exists(StatePath)) File.Replace(temp, StatePath, null);
                        else File.Move(temp, StatePath);
                        break;
                    }
                    catch (IOException) when (attempt < 5) { Thread.Sleep(10 * (attempt + 1)); }
                }
            }
            catch (Exception ex) { persistenceFault = ex; throw; }
            finally
            {
                try { if (File.Exists(temp)) File.Delete(temp); }
                catch (IOException) { /* Preserve the original failure; never replay the operation. */ }
                catch (UnauthorizedAccessException) { /* Cleanup cannot grant access or clear quarantine. */ }
            }
        }

        static void HoldRefresh()
        {
            if (refreshHeld) return;
            AssetDatabase.DisallowAutoRefresh();
            refreshHeld = true;
        }

        static void DropRefreshHold()
        {
            if (!refreshHeld) return;
            AssetDatabase.AllowAutoRefresh();
            refreshHeld = false;
        }

        static void BeforeReload()
        {
            if (state?.Data["import"] is JObject import && (bool?)import["requiresReload"] == true
                && ((string)import["state"] == "running" || (string)import["state"] == "settling"))
            { import["state"] = "reloading"; Save(state.Data); }
            DropRefreshHold();
        }

        static void AfterReload()
        {
            try { ResumeAfterReload(); }
            catch (Exception ex) { Debug.LogError("EDITOR_QUEUE_STATE_UNAVAILABLE: " + ex.Message); }
        }

        static void ResumeAfterReload()
        {
            // Load the persisted snapshot before inspecting its provenance fields.
            var current = State;
            if (EditorIdleWithoutImport() && current.Data["import"] is JObject import
                && (string)import["state"] == "reloading"
                && loadedImportState == "reloading"
                && (string)import["originBootUtc"] != BootUtc.ToString("o")
                && (string)import["operation"] == loadedImportOperation && loadedStateWriteUtc < BootUtc)
            { import["state"] = "complete"; Save(current.Data); }
            if (current.Holder != null) HoldRefresh();
        }

        static bool Idle()
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode || EditorApplication.isPlaying
                || EditorApplication.isCompiling || EditorApplication.isUpdating) return false;
            for (int i = 0; i < UnityEditor.SceneManagement.EditorSceneManager.sceneCount; i++)
                if (UnityEditor.SceneManagement.EditorSceneManager.GetSceneAt(i).isDirty) return false;
            return State.Data["import"] == null || (string)State.Data["import"]["state"] == "complete";
        }

        public static bool MayInvoke(string command)
        {
            if (command == "editor_queue") return Scope.Value == null;
            var scope = Scope.Value;
            if (scope == null || !scope.Active) return false;
            try
            {
                State.Authenticate(scope.Token);
                return (string)State.Operation?["id"] == scope.Operation
                    && (string)State.Operation?["state"] == "pending";
            }
            catch { return false; }
        }

        public static object Denied() => new ErrorResponse("EDITOR_QUEUE_REQUIRED",
            new { instruction = "Use execute_custom_tool(editor_queue): request, claim, execute, ack, release." });

        static JObject Snapshot(string token)
        {
            var snapshot = State.Status(token);
            var scene = UnityEditor.SceneManagement.EditorSceneManager.GetActiveScene();
            snapshot["editor"] = new JObject { ["playing"] = EditorApplication.isPlaying,
                ["changing"] = EditorApplication.isPlayingOrWillChangePlaymode,
                ["compiling"] = EditorApplication.isCompiling, ["updating"] = EditorApplication.isUpdating,
                ["scene"] = scene.path, ["dirty"] = scene.isDirty, ["idle"] = Idle(),
                ["pid"] = System.Diagnostics.Process.GetCurrentProcess().Id };
            snapshot["import"] = State.Data["import"]?.DeepClone();
            snapshot["autoRefreshHeld"] = refreshHeld;
            snapshot["importScheduler"] = "synchronous-refresh-v3";
            snapshot["persistenceVersion"] = "fail-closed-v2";
            snapshot["stagedFileVersion"] = "sha256-v1";
            snapshot["gateMvid"] = typeof(EditorQueueGateway).Module.ModuleVersionId.ToString();
            snapshot["bootUtc"] = BootUtc.ToString("o");
            snapshot["stateLoadedWriteUtc"] = loadedStateWriteUtc.ToString("o");
            return snapshot;
        }

        public static async Task<object> HandleCommand(JObject parameters)
        {
            var p = parameters ?? new JObject();
            string action = (string)p["action"];
            string token = (string)p["token"];
            try
            {
                // A delayCall may never run in an unfocused Editor. The next command can
                // finish only a persisted pre-boot reload, using the same idle checks.
                ResumeAfterReload();
                switch (action)
                {
                    case "status": return new SuccessResponse("Editor queue status.", Snapshot(token));
                    case "request": return new SuccessResponse("Queued; keep token private.",
                        State.Request((string)p["owner"], (double?)p["seconds"] ?? 600));
                    case "claim":
                        var holder = State.Claim(token, Idle()); HoldRefresh();
                        return new SuccessResponse("Editor phase claimed.", holder);
                    case "cancel": State.Cancel(token); break;
                    case "renew": State.Renew(token); break;
                    case "ack": State.Acknowledge(token, (string)p["operation"]); break;
                    case "recover_expired": State.RecoverExpired(token, Idle()); break;
                    case "reconcile_import":
                        return ReconcileImport(p, token);
                    case "release":
                        State.Release(token, Idle()); State.Data.Remove("import"); Save(State.Data); DropRefreshHold(); break;
                    case "execute": return await Execute(p, false);
                    case "write_files": return await Execute(p, true);
                    case "import":
                        State.Authenticate(token);
                        if (State.Operation != null && (string)State.Operation["state"] != "acknowledged")
                            return new ErrorResponse("EDITOR_QUEUE_RECEIPT_REQUIRED");
                        var import = State.Data["import"] as JObject;
                        if (import == null || (string)import["state"] != "staged" || !EditorIdleWithoutImport())
                            return new ErrorResponse("EDITOR_QUEUE_IMPORT_NOT_READY");
                        import["state"] = "scheduled"; Save(State.Data);
                        // delayCall depends on an Inspector update and can stall indefinitely
                        // while the Editor is unfocused even though MCP commands still run.
                        // Persist intent first, refresh on this main-thread command, then let
                        // compilation/reload finish asynchronously under the same holder.
                        RunImport();
                        return new SuccessResponse("Import requested once; poll status. Do not retry.");
                    default: return new ErrorResponse("EDITOR_QUEUE_UNKNOWN_ACTION");
                }
                return new SuccessResponse("Editor queue updated.");
            }
            catch (Exception ex) { return new ErrorResponse(ex.Message); }
        }

        static bool EditorIdleWithoutImport()
        {
            if (EditorApplication.isPlayingOrWillChangePlaymode || EditorApplication.isPlaying
                || EditorApplication.isCompiling || EditorApplication.isUpdating) return false;
            for (int i = 0; i < UnityEditor.SceneManagement.EditorSceneManager.sceneCount; i++)
                if (UnityEditor.SceneManagement.EditorSceneManager.GetSceneAt(i).isDirty) return false;
            return true;
        }

        static void RunImport()
        {
            var import = State.Data["import"] as JObject;
            if (import == null || (string)import["state"] != "scheduled") return;
            if (!EditorIdleWithoutImport())
            { import["state"] = "blocked_before_import"; Save(State.Data); return; }
            import["state"] = "running"; Save(State.Data);
            importRunning = true;
            DropRefreshHold();
            try
            {
                AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
                import["state"] = (bool)import["requiresReload"] ? "settling" : "complete";
                Save(State.Data);
            }
            catch (Exception ex) { import["state"] = "unknown"; import["error"] = ex.ToString(); Save(State.Data); }
            finally { importRunning = false; if (State.Holder != null) HoldRefresh(); }
        }

        static object ReconcileImport(JObject p, string token)
        {
            // Explicit owner reconciliation, never called by polling or timeout recovery.
            // An unknown arbitrary command cannot use this path. Require the failed import
            // to have been persisted before this domain boot and verify its actual targets.
            State.Authenticate(token, true);
            var import = State.Data["import"] as JObject;
            var files = State.Operation?["result"]?["data"]?["files"] as JArray;
            var expected = p["expectedFiles"] as JObject;
            var types = p["expectedTypes"] as JObject;
            if (importRunning || !EditorIdleWithoutImport() || import == null || (string)import["state"] != "unknown"
                || loadedStateWriteUtc >= BootUtc || (string)State.Operation?["state"] != "acknowledged"
                || (string)import["operation"] != loadedImportOperation
                || loadedImportState != "unknown"
                || (string)import["originBootUtc"] == BootUtc.ToString("o")
                || (string)State.Operation?["id"] != (string)import["operation"]
                || (string)p["operation"] != (string)import["operation"] || files == null || files.Count == 0
                || expected == null || types == null || types.Count == 0 || string.IsNullOrWhiteSpace((string)p["evidence"]))
                return new ErrorResponse("EDITOR_QUEUE_IMPORT_RECONCILIATION_REFUSED");
            foreach (string path in files.Values<string>())
            {
                string full = Path.Combine(Root, path);
                string actual = File.Exists(full) ? FileHash(full) : "missing";
                if ((string)expected[path] != actual) return new ErrorResponse("EDITOR_QUEUE_RECONCILIATION_FILE_MISMATCH");
            }
            foreach (var item in types.Properties())
            {
                bool loaded = AppDomain.CurrentDomain.GetAssemblies().Any(a => a.GetType(item.Name) != null);
                if (item.Value.Type != JTokenType.Boolean || loaded != (bool)item.Value)
                    return new ErrorResponse("EDITOR_QUEUE_RECONCILIATION_TYPE_MISMATCH");
            }
            import["reconciliation"] = new JObject { ["evidence"] = p["evidence"],
                ["expectedFiles"] = expected.DeepClone(), ["expectedTypes"] = types.DeepClone(),
                ["gateMvid"] = typeof(EditorQueueGateway).Module.ModuleVersionId.ToString(), ["utc"] = DateTime.UtcNow.ToString("o") };
            import["state"] = "complete";
            Save(State.Data);
            return new SuccessResponse("Import reconciled from verified files/types after domain reload. No import was retried.");
        }

        static async Task<object> Execute(JObject p, bool write)
        {
            string token = (string)p["token"], operation = (string)p["operation"];
            if (Scope.Value != null) return Denied();
            State.Authenticate(token);
            if (State.Data["import"] != null && (string)State.Data["import"]["state"] != "complete"
                && !(write && (string)State.Data["import"]["state"] == "compile_failed"))
                return new ErrorResponse("EDITOR_QUEUE_IMPORT_PENDING");
            if (EditorApplication.isCompiling || EditorApplication.isUpdating)
                return new ErrorResponse("EDITOR_QUEUE_EDITOR_BUSY");
            // Validate every file before recording an operation or touching any destination.
            var files = write ? ValidateFiles(p["files"] as JArray) : null;
            string command = (string)p["command"];
            if (!write && (string.IsNullOrWhiteSpace(command) || command == "editor_queue")) return Denied();
            var payload = (JObject)p.DeepClone(); payload.Remove("token");
            State.Start(token, operation, EditorQueueState.Hash(payload.ToString()));
            var permit = new Permit { Token = token, Operation = operation, Active = true };
            Scope.Value = permit;
            try
            {
                object result = write ? ApplyFiles(files, operation) :
                    await CommandRegistry.InvokeCommandAsync(command, p["parameters"] as JObject ?? new JObject());
                var receipt = result == null ? JValue.CreateNull() : JToken.FromObject(result);
                State.Complete(token, operation, receipt);
                return new SuccessResponse("Command complete; acknowledge receipt before continuing.",
                    new { operation, result = receipt });
            }
            catch (Exception ex)
            {
                State.Unknown(token, "command_outcome_unknown");
                return new ErrorResponse("EDITOR_QUEUE_OUTCOME_UNKNOWN", new { operation, error = ex.Message });
            }
            finally { permit.Active = false; Scope.Value = null; }
        }

        sealed class FileEdit { public string Relative, Full; public byte[] Bytes; public bool Delete; }
        static FileEdit[] ValidateFiles(JArray files)
        {
            if (!EditorIdleWithoutImport()) throw new InvalidOperationException("EDITOR_QUEUE_EDITOR_NOT_IDLE");
            if (files == null || files.Count < 1 || files.Count > 32) throw new InvalidOperationException("EDITOR_QUEUE_FILE_LIMIT");
            long total = 0;
            var edits = files.Select(item =>
            {
                string relative = (string)item["path"];
                if (string.IsNullOrEmpty(relative) || !relative.StartsWith("Assets/", StringComparison.Ordinal)
                    || relative.Contains("\\") || relative.Contains(":") || relative.Split('/').Any(s => s == ".." || s == "." || s == ""))
                    throw new InvalidOperationException("EDITOR_QUEUE_PATH_DENIED");
                string full = Path.GetFullPath(Path.Combine(Root, relative));
                for (var node = full; node.Length > Root.Length; node = Path.GetDirectoryName(node))
                    if ((File.Exists(node) || Directory.Exists(node)) && (File.GetAttributes(node) & FileAttributes.ReparsePoint) != 0)
                        throw new InvalidOperationException("EDITOR_QUEUE_REPARSE_PATH_DENIED");
                bool exists = File.Exists(full);
                string actual = exists ? FileHash(full) : "missing";
                if ((string)item["expectedSha256"] != actual) throw new InvalidOperationException("EDITOR_QUEUE_FILE_CHANGED");
                bool delete = (bool?)item["delete"] == true;
                if (delete && !exists) throw new InvalidOperationException("EDITOR_QUEUE_FILE_MISSING");
                bool staged = item["stagedPath"] != null;
                if ((delete && (staged || item["base64"] != null)) ||
                    (!delete && (staged == (item["base64"] != null))))
                    throw new InvalidOperationException("EDITOR_QUEUE_FILE_SOURCE_REQUIRED");
                byte[] bytes = delete ? null : staged ? ReadStagedFile(item) : Convert.FromBase64String((string)item["base64"]);
                total += bytes?.LongLength ?? 0;
                if (total > 10 * 1024 * 1024) throw new InvalidOperationException("EDITOR_QUEUE_FILE_BYTES_LIMIT");
                return new FileEdit { Relative = relative, Full = full, Bytes = bytes, Delete = delete };
            }).ToArray();
            if (edits.Select(x => x.Full).Distinct(StringComparer.OrdinalIgnoreCase).Count() != edits.Length)
                throw new InvalidOperationException("EDITOR_QUEUE_DUPLICATE_PATH");
            return edits;
        }

        static string FileHash(string path)
        {
            using var hash = System.Security.Cryptography.SHA256.Create();
            using var stream = File.OpenRead(path);
            return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }

        static byte[] ReadStagedFile(JToken item)
        {
            string relative = (string)item["stagedPath"];
            if (string.IsNullOrEmpty(relative) || !relative.StartsWith("JobQueue/payloads/", StringComparison.Ordinal)
                || relative.Contains("\\") || relative.Contains(":") || relative.Split('/').Any(s => s == ".." || s == "." || s == ""))
                throw new InvalidOperationException("EDITOR_QUEUE_STAGED_PATH_DENIED");
            string full = Path.GetFullPath(Path.Combine(Root, relative));
            for (var node = full; node.Length > Root.Length; node = Path.GetDirectoryName(node))
                if ((File.Exists(node) || Directory.Exists(node)) && (File.GetAttributes(node) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidOperationException("EDITOR_QUEUE_REPARSE_PATH_DENIED");
            var info = new FileInfo(full);
            if (!info.Exists) throw new InvalidOperationException("EDITOR_QUEUE_STAGED_FILE_MISSING");
            if (info.Length > 10 * 1024 * 1024) throw new InvalidOperationException("EDITOR_QUEUE_FILE_BYTES_LIMIT");
            // Hash the exact bytes retained for ApplyFiles, not a second read of a mutable path.
            byte[] bytes = File.ReadAllBytes(full);
            using var hash = System.Security.Cryptography.SHA256.Create();
            string actual = BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
            if ((string)item["stagedSha256"] != actual) throw new InvalidOperationException("EDITOR_QUEUE_STAGED_FILE_CHANGED");
            return bytes;
        }

        static object ApplyFiles(FileEdit[] files, string operation)
        {
            // UUID prevents user operation labels from becoming filesystem paths.
            string backup = Path.Combine(Root, "Backups", "editor-queue-writes", Guid.NewGuid().ToString("N"));
            foreach (var file in files)
            {
                string copy = Path.Combine(backup, file.Relative);
                Directory.CreateDirectory(Path.GetDirectoryName(copy));
                if (File.Exists(file.Full)) File.Copy(file.Full, copy, false);
                else File.WriteAllText(copy + ".was-missing", "Destination did not exist before this transaction.");
            }
            bool reload = files.Any(f => new[] { ".cs", ".asmdef", ".asmref", ".dll", ".rsp" }
                .Contains(Path.GetExtension(f.Relative).ToLowerInvariant()));
            State.Data["import"] = new JObject { ["state"] = "staged", ["requiresReload"] = reload,
                ["operation"] = operation, ["backup"] = backup, ["originBootUtc"] = BootUtc.ToString("o") };
            Save(State.Data);
            foreach (var file in files)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(file.Full));
                if (file.Delete) File.Delete(file.Full);
                else
                {
                    string temp = Path.Combine(backup, Guid.NewGuid().ToString("N") + ".tmp");
                    File.WriteAllBytes(temp, file.Bytes);
                    if (File.Exists(file.Full)) File.Replace(temp, file.Full, null);
                    else File.Move(temp, file.Full);
                }
            }
            return new SuccessResponse("Files written with backups; acknowledge then import while holding this phase.",
                new { files = files.Select(x => x.Relative).ToArray(), backup, requiresReload = reload });
        }
    }
}
