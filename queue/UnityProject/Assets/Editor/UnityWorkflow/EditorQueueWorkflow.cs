// Explicit decorator of the documented duplicate-name registration seam. Both
// CommandRegistry and ToolDiscoveryService sort by full name; Z sorts after MCP.
// All authority, receipts, and original actions remain in EditorQueueGateway.
using System;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace ZUnityWorkflow
{
    [McpForUnityTool("editor_queue", Description = Description, RequiresPolling = true, PollAction = "workflow_poll", MaxPollSeconds = 1800)]
    public static class EditorQueueWorkflow
    {
        public const string Description = "Exclusive Editor queue with central workflow runner. Begin with workflow_capabilities to register native polling. For files/check or scene/Play use workflow_run, mode job/phase, stable submission_key and staged manifest_path. Waiting, claims, receipts, own cleanup and release run centrally. Recover with workflow_result and the same key. Legacy actions delegate unchanged. Never ask the user to chase another owner.";
        static readonly HttpClient Client = new HttpClient { Timeout = TimeSpan.FromSeconds(8) };
        static readonly SemaphoreSlim Startup = new SemaphoreSlim(1, 1);
        static readonly string Root = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
        // Suppress beforefieldinit: capture Application.dataPath at the initial
        // main-thread handler entry, never on a later HTTP worker thread.
        static EditorQueueWorkflow() { }

        public static async Task<object> HandleCommand(JObject p)
        {
            var action = (string)p?["action"] ?? "";
            if (!action.StartsWith("workflow_", StringComparison.Ordinal))
                return await EditorQueueGateway.HandleCommand(p);
            // CommandRegistry also captures Unity's SynchronizationContext when
            // awaiting async handlers. Finish this short HTTP exchange on entry;
            // long queue/Play waits remain in the service and server-side poller.
            // No Unity API is accessed by the background HTTP operation.
            using var budget = new CancellationTokenSource(TimeSpan.FromSeconds(20));
            var work = Task.Run(() => Workflow(p, budget.Token));
            if (!work.Wait(TimeSpan.FromSeconds(21)))
                return new ErrorResponse("WORKFLOW_TRANSPORT_UNAVAILABLE: ingress deadline. Recover using workflow_result and the same key.");
            return work.GetAwaiter().GetResult();
        }

        static async Task<object> Workflow(JObject p, CancellationToken cancellation)
        {
            var action = (string)p["action"];
            try
            {
                if (action == "workflow_capabilities") await RegisterPolling(cancellation);
                JObject reply;
                try
                {
                    Validate(await Post(new JObject { ["action"] = "workflow_capabilities" }, cancellation));
                    reply = await Post(p, cancellation);
                }
                catch (HttpRequestException)
                {
                    await Startup.WaitAsync(cancellation);
                    try
                    {
                        // Only our fixed local service, never Unity or another worker.
                        try { reply = await Post(new JObject { ["action"] = "workflow_capabilities" }, cancellation); }
                        catch (HttpRequestException)
                        {
                            // Match queue/install.py's environment in this project.
                            // Never fall back to a machine-wide Python with different dependencies.
                            var python = Path.Combine(Root, ".unity-mcp-queue", "venv", "Scripts", "python.exe");
                            var server = Path.Combine(Root, "Tools", "UnityWorkflowMcp", "server.py");
                            if (!File.Exists(python))
                                throw new FileNotFoundException("WORKFLOW_PYTHON_MISSING: expected " + python
                                    + ". Complete or repair this project's queue installation before starting the service.");
                            if (!File.Exists(server))
                                throw new FileNotFoundException("WORKFLOW_SERVER_MISSING: expected " + server
                                    + ". Restore the queue tools for this project before starting the service.");
                            var start = new ProcessStartInfo(python,
                                "-s -B \"" + server + "\" --transport http --port 8091")
                            { WorkingDirectory = Root, UseShellExecute = false, CreateNoWindow = true,
                              WindowStyle = ProcessWindowStyle.Hidden };
                            Process.Start(start);
                            reply = null;
                            for (int i = 0; i < 30 && reply == null; i++)
                            {
                                await Task.Delay(200, cancellation);
                                try { reply = await Post(new JObject { ["action"] = "workflow_capabilities" }, cancellation); }
                                catch (HttpRequestException) { }
                            }
                            if (reply == null) throw new Exception("Workflow service startup unavailable");
                        }
                        // The first request was not accepted if connection failed.
                        // Submit is idempotent by key if the response boundary was lost.
                        Validate(reply);
                        reply = await Post(p, cancellation);
                    }
                    finally { Startup.Release(); }
                }
                if ((bool?)reply["success"] != true) return reply;
                var data = reply["data"] as JObject;
                if (action == "workflow_capabilities") return reply;
                if ((bool?)data?["completed"] != true && (bool?)data?["attention_required"] != true)
                    return new PendingResponse("Central workflow continues; no owner intervention needed.", 1.0, data);
                return new JObject { ["success"] = (bool?)data?["verified_success"] == true,
                    ["message"] = "Central workflow result; inspect verified_success and evidence.", ["data"] = data };
            }
            catch (Exception error)
            { return new ErrorResponse("WORKFLOW_TRANSPORT_UNAVAILABLE: " + error.Message + ". Recover using workflow_result and the same key; do not replay with a new key."); }
        }

        static void Validate(JObject reply)
        {
            var data = reply?["data"] as JObject;
            if ((bool?)reply?["success"] != true || data == null
                || !string.Equals(Path.GetFullPath((string)data["workspace"] ?? ""), Root, StringComparison.OrdinalIgnoreCase)
                || (string)data["native_ingress"] != "editor_queue.workflow_run")
                throw new Exception("Workflow endpoint does not match this workspace/ingress");
        }

        static async Task RegisterPolling(CancellationToken cancellation)
        {
            // This documented local registration endpoint gives the project
            // definition precedence over stale global tool metadata. It does
            // not reconnect Unity or alter gate/phase ownership.
            string project;
            using (var sha = System.Security.Cryptography.SHA1.Create())
                project = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(
                    Path.Combine(Root, "Assets").Replace('\\', '/')))).Replace("-", "").ToLowerInvariant().Substring(0, 16);
            var payload = new JObject { ["project_id"] = project, ["project_hash"] = project,
                ["tools"] = new JArray(new JObject { ["name"] = "editor_queue", ["description"] = Description,
                    ["structured_output"] = true, ["requires_polling"] = true, ["poll_action"] = "workflow_poll",
                    ["max_poll_seconds"] = 1800, ["parameters"] = new JArray() }) };
            var reply = await Post(payload, cancellation, "http://127.0.0.1:8080/register-tools");
            if ((bool?)reply["success"] != true) throw new Exception("Native polling registration failed");
        }

        static async Task<JObject> Post(JObject p, CancellationToken cancellation, string url = "http://127.0.0.1:8091/workflow")
        {
            using var content = new StringContent(p.ToString(), Encoding.UTF8, "application/json");
            using var response = await Client.PostAsync(url, content, cancellation);
            response.EnsureSuccessStatusCode();
            return JObject.Parse(await response.Content.ReadAsStringAsync());
        }
    }
}
