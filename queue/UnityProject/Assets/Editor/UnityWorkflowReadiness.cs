using System;
using System.IO;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace UnityWorkflow
{
    // Readiness hints only. The unchanged queue authority still checks live
    // readiness, FIFO and tokens on every claim. No scene or gate mutations.
    [InitializeOnLoad]
    public static class ReadinessNotification
    {
        public const string Version = "preclaim-notification-v1";
        static readonly string Root = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
        static readonly string Epoch = Guid.NewGuid().ToString("N");
        static readonly int Pid = System.Diagnostics.Process.GetCurrentProcess().Id;
        public const string SelfHeal = "foreign-rewrite-v1";
        static readonly string Target = Path.Combine(Root, "Library", "UnityWorkflowSignals", "editor-readiness.json");
        static string previous;
        static long sequence;
        static bool warned;
        static DateTime written;
        static double nextCheck;

        static ReadinessNotification()
        {
            // Asset import workers (batchMode children named AssetImportWorker*) load this script too; their pid
            // overwrote the Editor's signal and every claim/initial wait refused the mismatch (28 Oct 2026).
            if (AssetDatabase.IsAssetImportWorkerProcess()) return;
            EditorApplication.update += Update;
            AssemblyReloadEvents.beforeAssemblyReload += () => Publish(true);
            EditorApplication.quitting += () => Publish(true);
        }

        static void Update()
        {
            // Not a heartbeat: rewrite only when another process replaced or removed our
            // file, so an idle Editor never leaves a foreign pid behind (28 Oct 2026 race).
            double now = EditorApplication.timeSinceStartup;
            if (previous != null && now >= nextCheck)
            {
                nextCheck = now + 0.5;
                try { if (File.GetLastWriteTimeUtc(Target) != written) previous = null; }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
            Publish(false);
        }

        static void Publish(bool unavailable)
        {
            bool dirty = false;
            for (int i = 0; i < EditorSceneManager.sceneCount; i++)
                dirty |= EditorSceneManager.GetSceneAt(i).isDirty;
            bool playing = EditorApplication.isPlaying;
            bool changing = EditorApplication.isPlayingOrWillChangePlaymode;
            bool compiling = EditorApplication.isCompiling;
            bool updating = EditorApplication.isUpdating;
            // No timestamp heartbeat. Only a readiness transition or a foreign rewrite writes a file.
            string signature = $"{unavailable}:{dirty}:{playing}:{changing}:{compiling}:{updating}";
            if (signature == previous) return;
            string target = Target;
            string directory = Path.GetDirectoryName(target);
            string temporary = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                Directory.CreateDirectory(directory);
                var value = new JObject {
                    ["schema"] = Version, ["workspace"] = Root, ["pid"] = Pid,
                    ["epoch"] = Epoch, ["sequence"] = sequence + 1,
                    ["idle"] = !(unavailable || dirty || playing || changing || compiling || updating),
                    ["unavailable"] = unavailable, ["dirty"] = dirty, ["playing"] = playing,
                    ["changing"] = changing, ["compiling"] = compiling, ["updating"] = updating
                };
                using (var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                using (var writer = new StreamWriter(file))
                { writer.Write(value.ToString()); writer.Flush(); file.Flush(true); }
                if (File.Exists(target)) File.Replace(temporary, target, null);
                else File.Move(temporary, target);
                written = File.GetLastWriteTimeUtc(target);
                previous = signature;
                sequence++;
                warned = false;
            }
            catch (Exception error) when (error is IOException || error is UnauthorizedAccessException)
            {
                if (!warned) Debug.LogWarning("Unity workflow readiness hint unavailable: " + error.Message);
                warned = true;
            }
            finally
            {
                try { if (File.Exists(temporary)) File.Delete(temporary); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
            }
        }
    }
}
