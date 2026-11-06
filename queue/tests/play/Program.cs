// Execute actual linked helper; replace only Unity and gateway boundaries.
using System;
using Newtonsoft.Json.Linq;
using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityWorkflow;

static class Program
{
    static int checks;
    static void Require(bool condition, string message)
    {
        checks++;
        if (!condition) throw new Exception(message);
    }
    static object Call(string action, string job = "job-a") =>
        WorkflowPlay.HandleCommand(new JObject { ["action"] = action, ["job"] = job });
    static void Reset()
    {
        EditorQueueGateway.Allowed = true;
        EditorApplication.isPlaying = false;
        EditorApplication.isPaused = false;
        EditorApplication.isPlayingOrWillChangePlaymode = false;
        EditorSceneManager.Dirty = false;
        SessionState.Marker = "";
    }
    static void RejectedWithoutMutation(string reason, Func<object> action)
    {
        var before = (EditorApplication.isPlaying, EditorApplication.isPaused, SessionState.Marker);
        Require(action() is ErrorResponse, reason + " was accepted");
        Require(before == (EditorApplication.isPlaying, EditorApplication.isPaused, SessionState.Marker),
            reason + " changed editor state");
    }
    static void Main()
    {
        Reset();
        EditorQueueGateway.Allowed = false;
        RejectedWithoutMutation("outside gateway start", () => Call("start"));
        RejectedWithoutMutation("outside gateway stop", () => Call("stop"));
        Reset();
        RejectedWithoutMutation("null parameters", () => WorkflowPlay.HandleCommand(null));
        RejectedWithoutMutation("empty job", () => Call("start", ""));
        RejectedWithoutMutation("blank job", () => Call("stop", " "));
        RejectedWithoutMutation("unknown action", () => Call("other"));
        EditorSceneManager.Dirty = true;
        RejectedWithoutMutation("dirty scene", () => Call("start"));
        Reset();
        EditorApplication.isPlayingOrWillChangePlaymode = true;
        RejectedWithoutMutation("already playing/changing", () => Call("start"));
        Reset();
        var start = Call("start") as SuccessResponse;
        Require(start != null && (string)JObject.FromObject(start.Data)["result"] == "start-requested", "start result changed");
        Require(EditorApplication.isPlaying && SessionState.Marker == "job-a", "start must set owned marker and Play");
        EditorApplication.isPaused = true;
        RejectedWithoutMutation("foreign marker", () => Call("stop", "job-b"));
        var stop = Call("stop") as SuccessResponse;
        Require(stop != null && (string)JObject.FromObject(stop.Data)["result"] == "stop-requested", "stop result changed");
        Require(!EditorApplication.isPlaying && !EditorApplication.isPaused, "owned stop must unpause and stop");
        Require(SessionState.Marker == "job-a", "stop must retain marker until transition wait completes");
        Reset();
        RejectedWithoutMutation("missing marker", () => Call("stop"));
        // A second job must receive its own data; no source compilation/cache key involved.
        Require(Call("start", "job-b") is SuccessResponse && SessionState.Marker == "job-b", "second job owner was stale");
        Console.WriteLine("PASS " + checks + " lifecycle assertions (linked source; stubbed Unity/gateway)");
    }
}

namespace MCPForUnity.Editor.Helpers
{
    [AttributeUsage(AttributeTargets.Class)]
    public sealed class McpForUnityToolAttribute : Attribute
    {
        public McpForUnityToolAttribute(string name) { }
        public string Description { get; set; }
    }
    public sealed class ErrorResponse { public ErrorResponse(string message) { } }
    public sealed class SuccessResponse
    {
        public object Data;
        public SuccessResponse(string message, object data) { Data = data; }
    }
}
namespace MCPForUnity.Editor.Tools
{
    public static class EditorQueueGateway
    {
        public static bool Allowed;
        public static bool MayInvoke(string command) => Allowed && command == "workflow_play";
        public static object Denied() => new ErrorResponse("EDITOR_QUEUE_REQUIRED");
    }
}
namespace UnityEditor
{
    public static class EditorApplication
    {
        public static bool isPlaying, isPaused, isPlayingOrWillChangePlaymode;
    }
    public static class SessionState
    {
        public static string Marker;
        public static void SetString(string key, string value) { Marker = value; }
        public static string GetString(string key, string fallback) => Marker;
    }
}
namespace UnityEditor.SceneManagement
{
    public struct Scene { public bool isDirty; }
    public static class EditorSceneManager
    {
        public static bool Dirty;
        public static Scene GetActiveScene() => new Scene { isDirty = Dirty };
    }
}
