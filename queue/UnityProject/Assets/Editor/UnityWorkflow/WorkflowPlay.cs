using MCPForUnity.Editor.Helpers;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;

namespace UnityWorkflow
{
    // Fixed lifecycle operations avoid compiling a new snippet for every job ID.
    // The gateway still owns authorization, pending receipts and acknowledgement.
    [McpForUnityTool("workflow_play", Description = "Internal queue Play transitions; invoke through editor_queue execute.")]
    public static class WorkflowPlay
    {
        public static object HandleCommand(JObject parameters)
        {
            if (!EditorQueueGateway.MayInvoke("workflow_play"))
                return EditorQueueGateway.Denied();
            var job = (string)parameters?["job"];
            if (string.IsNullOrWhiteSpace(job))
                return new ErrorResponse("Play job is required");
            switch ((string)parameters?["action"])
            {
                case "start":
                    if (EditorApplication.isPlayingOrWillChangePlaymode)
                        return new ErrorResponse("Already playing");
                    if (EditorSceneManager.GetActiveScene().isDirty)
                        return new ErrorResponse("Scene dirty");
                    SessionState.SetString("UnityWorkflow.PlayOwner", job);
                    EditorApplication.isPlaying = true;
                    return new SuccessResponse("Play start requested", new { result = "start-requested" });
                case "stop":
                    if (SessionState.GetString("UnityWorkflow.PlayOwner", "") != job)
                        return new ErrorResponse("Play owner mismatch");
                    EditorApplication.isPaused = false;
                    EditorApplication.isPlaying = false;
                    return new SuccessResponse("Play stop requested", new { result = "stop-requested" });
                default:
                    return new ErrorResponse("Unknown Play action");
            }
        }
    }
}
