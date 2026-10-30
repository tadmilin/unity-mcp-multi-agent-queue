using System;
using System.Collections.Generic;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json.Linq;

namespace MCPForUnity.Editor.Tools
{
    // Unity-independent authority. Only token hashes are persisted. Owner labels are not credentials.
    public sealed class EditorQueueState
    {
        public JObject Data { get; }
        readonly Action<JObject> save;
        readonly Func<double> clock;
        JObject durable;
        bool persistenceFailed;

        public EditorQueueState(JObject data, Action<JObject> save, Func<double> clock)
        {
            Data = data ?? new JObject { ["version"] = 1, ["next"] = 0, ["waiting"] = new JArray() };
            if ((int?)Data["version"] != 1 || Data["waiting"] is not JArray)
                throw new InvalidOperationException("EDITOR_QUEUE_STATE_INVALID");
            this.save = save;
            this.clock = clock;
            durable = (JObject)Data.DeepClone();
        }

        public static string Hash(string value)
        {
            using var hash = SHA256.Create();
            return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(value ?? ""))).Replace("-", "").ToLowerInvariant();
        }

        static void Need(bool condition, string code)
        {
            if (!condition) throw new InvalidOperationException(code);
        }

        void EnsurePersistence()
        {
            Need(!persistenceFailed, "EDITOR_QUEUE_PERSISTENCE_FAILED_RELOAD_REQUIRED");
        }

        void Save()
        {
            EnsurePersistence();
            try { save(Data); durable = (JObject)Data.DeepClone(); }
            catch
            {
                // A failed release/claim must not publish ownership that was never durable.
                Data.RemoveAll();
                foreach (var property in durable.Properties()) Data.Add(property.Name, property.Value.DeepClone());
                persistenceFailed = true;
                throw;
            }
        }
        public JObject Holder => Data["holder"] as JObject;
        public JObject Operation => Holder?["operation"] as JObject;

        public void Sweep()
        {
            EnsurePersistence();
            bool changed = false;
            foreach (var item in ((JArray)Data["waiting"]).OfType<JObject>().ToArray())
                if ((double)item["deadline"] <= clock()) { item.Remove(); changed = true; }
            if (Holder != null && (double)Holder["deadline"] <= clock() && Holder["blocked"] == null)
            { Holder["blocked"] = "owner_deadline_expired"; changed = true; }
            if (changed) Save();
        }

        public JObject Request(string owner, double seconds)
        {
            Need(!string.IsNullOrWhiteSpace(owner) && owner.Length <= 120, "EDITOR_QUEUE_OWNER_REQUIRED");
            Need(seconds >= 10 && seconds <= 1800 && !double.IsNaN(seconds), "EDITOR_QUEUE_BAD_DEADLINE");
            Sweep();
            Need(((JArray)Data["waiting"]).Count < 32, "EDITOR_QUEUE_FULL");
            byte[] bytes = new byte[32];
            using (var random = RandomNumberGenerator.Create()) random.GetBytes(bytes);
            string token = Convert.ToBase64String(bytes);
            long ticket = (long)Data["next"] + 1;
            Data["next"] = ticket;
            ((JArray)Data["waiting"]).Add(new JObject { ["ticket"] = ticket, ["owner"] = owner,
                ["secretHash"] = Hash(token), ["seconds"] = seconds, ["deadline"] = clock() + seconds });
            Save();
            return new JObject { ["ticket"] = ticket, ["token"] = token };
        }

        public JObject Claim(string token, bool idle)
        {
            Sweep();
            Need(Holder == null, "EDITOR_QUEUE_HELD");
            var first = ((JArray)Data["waiting"]).First as JObject;
            Need(first != null && (string)first["secretHash"] == Hash(token), "EDITOR_QUEUE_FIFO_OR_TOKEN");
            Need(idle, "EDITOR_QUEUE_EDITOR_NOT_IDLE");
            first.Remove();
            first["deadline"] = clock() + (double)first["seconds"];
            first["used"] = new JArray();
            Data["holder"] = first;
            Save();
            return PublicHolder();
        }

        public JObject Authenticate(string token, bool allowBlocked = false)
        {
            Sweep();
            Need(!string.IsNullOrEmpty(token) && Holder != null && (string)Holder["secretHash"] == Hash(token),
                "EDITOR_QUEUE_TOKEN_INVALID");
            Need(allowBlocked || Holder["blocked"] == null, "EDITOR_QUEUE_QUARANTINED");
            return Holder;
        }

        public void Start(string token, string operation, string digest)
        {
            var holder = Authenticate(token);
            Need(!string.IsNullOrEmpty(operation) && operation.Length <= 120, "EDITOR_QUEUE_OPERATION_REQUIRED");
            Need(Operation == null || (string)Operation["state"] == "acknowledged", "EDITOR_QUEUE_RECEIPT_REQUIRED");
            var used = (JArray)holder["used"];
            Need(!used.Values<string>().Contains(operation), "EDITOR_QUEUE_OPERATION_REUSED");
            Need(used.Count < 1000, "EDITOR_QUEUE_PHASE_LIMIT");
            used.Add(operation);
            holder["operation"] = new JObject { ["id"] = operation, ["sha256"] = digest, ["state"] = "pending" };
            Save(); // Must be durable before any side effect.
        }

        public void Complete(string token, string operation, JToken result)
        {
            Authenticate(token, true);
            Need((string)Operation?["id"] == operation && (string)Operation?["state"] == "pending",
                "EDITOR_QUEUE_NO_PENDING_OPERATION");
            Operation["result"] = result.DeepClone();
            Operation["state"] = "complete";
            Save();
        }

        public void Unknown(string token, string reason)
        {
            Authenticate(token, true);
            if (Operation != null) Operation["state"] = "unknown";
            Holder["blocked"] = reason;
            Save();
        }

        public void Acknowledge(string token, string operation)
        {
            Authenticate(token, true);
            Need((string)Operation?["id"] == operation && (string)Operation?["state"] == "complete",
                "EDITOR_QUEUE_TERMINAL_RECEIPT_REQUIRED");
            Operation["state"] = "acknowledged";
            Save();
        }

        public void Release(string token, bool idle)
        {
            Authenticate(token);
            Need(idle, "EDITOR_QUEUE_EDITOR_NOT_IDLE");
            Need(Operation == null || (string)Operation["state"] == "acknowledged", "EDITOR_QUEUE_RECEIPT_REQUIRED");
            Data.Remove("holder");
            Save();
        }

        public void Cancel(string token)
        {
            EnsurePersistence();
            var item = ((JArray)Data["waiting"]).OfType<JObject>().FirstOrDefault(x => (string)x["secretHash"] == Hash(token));
            Need(item != null, "EDITOR_QUEUE_WAITING_TOKEN_REQUIRED");
            item.Remove();
            Save();
        }

        public void Renew(string token)
        {
            Authenticate(token);
            Holder["deadline"] = clock() + (double)Holder["seconds"];
            Save();
        }

        // Recovery has no caller-supplied idle/terminal booleans. The adapter supplies live state.
        // Pending/unknown execution can never be cleared by this method.
        public void RecoverExpired(string token, bool idle)
        {
            Authenticate(token, true);
            Need((string)Holder["blocked"] == "owner_deadline_expired" && idle,
                "EDITOR_QUEUE_OPERATOR_RECONCILIATION_REQUIRED");
            Need(Operation == null || (string)Operation["state"] == "acknowledged",
                "EDITOR_QUEUE_TERMINAL_RECEIPT_REQUIRED");
            Holder.Remove("blocked");
            Holder["deadline"] = clock() + (double)Holder["seconds"];
            Save();
        }

        public JObject PublicHolder()
        {
            if (Holder == null) return null;
            return new JObject { ["ticket"] = Holder["ticket"], ["owner"] = Holder["owner"],
                ["deadline"] = Holder["deadline"], ["blocked"] = Holder["blocked"],
                ["operationState"] = Operation?["state"] };
        }

        public JObject Status(string token)
        {
            Sweep();
            var result = new JObject { ["holder"] = PublicHolder(), ["waiting"] = ((JArray)Data["waiting"]).Count };
            if (!string.IsNullOrEmpty(token))
            {
                Authenticate(token, true);
                result["receipt"] = Operation?.DeepClone();
            }
            return result;
        }
    }
}
