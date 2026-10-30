// Run the actual exported ingress class. Replace only external boundaries;
// this harness neither opens sockets nor launches a workflow service.
global using HttpClient = StartupProbe.ProbeHttpClient;
global using Process = StartupProbe.ProbeProcess;

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;

namespace StartupProbe
{
    static class Program
    {
        internal static string Root;
        internal static bool Online;
        internal static bool WrongWorkspace;
        internal static readonly List<ProcessStartInfo> Starts = new();

        static async Task<int> Main(string[] args)
        {
            var scenario = args.Length == 0 ? "offline" : args[0];
            var realHelp = scenario == "installed-help";
            Root = realHelp ? Path.GetFullPath(args[1])
                : Path.Combine(Path.GetTempPath(), "queue startup probe " + Guid.NewGuid().ToString("N"));
            if (!realHelp) Directory.CreateDirectory(Path.Combine(Root, "Assets"));
            var python = Path.Combine(Root, ".unity-mcp-queue", "venv", "Scripts", "python.exe");
            var server = Path.Combine(Root, "Tools", "UnityWorkflowMcp", "server.py");
            if (!realHelp)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(python));
                Directory.CreateDirectory(Path.GetDirectoryName(server));
                if (scenario != "missing-python" && scenario != "online") File.WriteAllText(python, "test placeholder: never executed");
                if (scenario != "missing-server") File.WriteAllText(server, "# test placeholder: never executed");
            }
            Online = scenario == "online" || scenario == "wrong-workspace";
            WrongWorkspace = scenario == "wrong-workspace";
            try
            {
                var request = new JObject { ["action"] = scenario == "capabilities" ? "workflow_capabilities" : "workflow_result",
                    ["mode"] = "phase", ["submission_key"] = "probe-stable-key" };
                var method = typeof(ZUnityWorkflow.EditorQueueWorkflow).GetMethod("Workflow", BindingFlags.Static | BindingFlags.NonPublic);
                var task = (Task<object>)method.Invoke(null, new object[] { request, CancellationToken.None });
                var result = await task;
                if (scenario == "offline" || scenario == "capabilities" || realHelp)
                {
                    Require(Starts.Count == 1, "service unavailable must launch once");
                    Require(Path.GetFullPath(Starts[0].FileName) == python,
                        "expected installer venv, got " + Starts[0].FileName);
                    Require(Starts[0].WorkingDirectory == Root, "wrong working directory");
                    Require(Starts[0].Arguments.Contains("\"" + server + "\""), "server path must be quoted");
                    Require(!Starts[0].UseShellExecute && Starts[0].CreateNoWindow, "service must start without a shell/window");
                    Require(result is JObject value && (bool?)value["success"] == true, "startup result did not succeed");
                    if (realHelp)
                    {
                        // Keep the exact selected interpreter/cwd/quoted script. --help
                        // exits before create_server(), so it cannot bind production ports.
                        var start = Starts[0];
                        start.Arguments += " --help";
                        start.RedirectStandardOutput = true;
                        start.RedirectStandardError = true;
                        start.Environment["DO_NOT_TRACK"] = "1";
                        start.Environment["FASTMCP_CHECK_FOR_UPDATES"] = "off";
                        using var child = System.Diagnostics.Process.Start(start);
                        var stdout = child.StandardOutput.ReadToEndAsync();
                        var stderr = child.StandardError.ReadToEndAsync();
                        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
                        try { await child.WaitForExitAsync(timeout.Token); }
                        catch (OperationCanceledException) { child.Kill(entireProcessTree: true); throw; }
                        Require(child.ExitCode == 0, "selected Python/script failed --help: " + await stderr);
                        Require((await stdout).Contains("--transport"), "workflow help output missing");
                    }
                }
                else if (scenario == "online")
                {
                    Require(Starts.Count == 0, "already running service must not launch another process");
                    Require(result is JObject value && (bool?)value["success"] == true, "online service should work without an installed interpreter");
                }
                else
                {
                    Require(Starts.Count == 0, "invalid startup must not launch a process");
                    Require(result is MCPForUnity.Editor.Helpers.ErrorResponse, "expected explicit error");
                    var error = ((MCPForUnity.Editor.Helpers.ErrorResponse)result).Message;
                    var expected = scenario == "missing-python" ? "WORKFLOW_PYTHON_MISSING"
                        : scenario == "missing-server" ? "WORKFLOW_SERVER_MISSING" : "workspace/ingress";
                    Require(error.Contains(expected), "missing diagnostic: " + expected + "; got " + error);
                    Require(error.Contains("same key"), "recovery key guidance lost");
                }
                Console.WriteLine("PASS " + scenario);
                return 0;
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("FAIL " + scenario + ": " + error.Message);
                return 1;
            }
            finally
            {
                // This process created this exact unique temporary fixture.
                if (!realHelp)
                {
                    if (Path.GetDirectoryName(Root) != Path.TrimEndingDirectorySeparator(Path.GetTempPath()))
                        throw new InvalidOperationException("Unexpected fixture cleanup path");
                    Directory.Delete(Root, true);
                }
            }
        }

        static void Require(bool condition, string reason)
        {
            if (!condition) throw new InvalidOperationException(reason);
        }
    }

    sealed class ProbeHttpClient
    {
        public TimeSpan Timeout { get; set; }
        public Task<HttpResponseMessage> PostAsync(string url, HttpContent content, CancellationToken cancellation)
        {
            if (url.EndsWith("/register-tools", StringComparison.Ordinal))
                return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("{\"success\":true}") });
            if (!Program.Online) throw new HttpRequestException("Test transport: service unavailable");
            var data = new JObject { ["workspace"] = Program.WrongWorkspace ? Program.Root + "-other" : Program.Root,
                ["native_ingress"] = "editor_queue.workflow_run", ["completed"] = true, ["verified_success"] = true };
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) {
                Content = new StringContent(new JObject { ["success"] = true, ["data"] = data }.ToString()) });
        }
    }

    static class ProbeProcess
    {
        public static void Start(ProcessStartInfo start)
        {
            Program.Starts.Add(start);
            Program.Online = true;
        }
    }
}

// Only Unity/package API surfaces used by the linked class are stubbed.
namespace UnityEngine
{
    static class Application { public static string dataPath => Path.Combine(StartupProbe.Program.Root, "Assets"); }
}
namespace MCPForUnity.Editor.Tools
{
    sealed class McpForUnityToolAttribute : Attribute
    {
        public McpForUnityToolAttribute(string name) { }
        public string Description { get; set; }
        public bool RequiresPolling { get; set; }
        public string PollAction { get; set; }
        public int MaxPollSeconds { get; set; }
    }
    static class EditorQueueGateway
    {
        public static Task<object> HandleCommand(JObject value) => throw new InvalidOperationException("Unexpected legacy gate call");
    }
}
namespace MCPForUnity.Editor.Helpers
{
    sealed class ErrorResponse { public string Message; public ErrorResponse(string message) { Message = message; } }
    sealed class PendingResponse { public PendingResponse(string message, double seconds, JObject data) { } }
}
