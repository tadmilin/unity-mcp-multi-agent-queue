using System.Globalization;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityWorkflow;

var results = new List<object>();
var passed = true;
var vector = new Vector3(0.25f, 1.5f, -2f);
var cases = new (string Name, object Value, string Path)[]
{
    ("right", new { right = vector }, "right"),
    ("scale", new { scale = vector }, "scale"),
    ("transforms", new { transforms = new[] { new { world = vector } } }, "transforms[0].world"),
};
bool SameVector(JToken actual, Vector3 expected) =>
    actual["x"].Value<float>() == expected.x && actual["y"].Value<float>() == expected.y
    && actual["z"].Value<float>() == expected.z && actual.Children().Count() == 3;
void Check(string name, bool success, object detail = null)
{
    passed &= success;
    results.Add(new { name, success, detail });
}

foreach (var item in cases)
{
    string failure = null;
    try { JsonConvert.SerializeObject(item.Value); }
    catch (JsonSerializationException error) { failure = error.Message; }
    Check(item.Name + "-baseline", failure != null && failure.Contains("Self referencing loop")
        && failure.Contains("UnityEngine.Vector3"), failure);
    var actual = JObject.Parse(WorkflowJson.Serialize(item.Value)).SelectToken(item.Path);
    Check(item.Name + "-helper", SameVector(actual, vector), actual);
}

var bounds = new Bounds(vector, new Vector3(4f, 5f, 6f));
var mixed = new { label = "evidence", count = 3, empty = (object)null,
    values = new[] { Vector3.zero, vector }, meshBounds = bounds, localBounds = bounds,
    restVertex = vector };
var document = JObject.Parse(WorkflowJson.Serialize(mixed));
Check("nested-bounds", SameVector(document["meshBounds"]["center"], bounds.center)
    && SameVector(document["meshBounds"]["size"], bounds.size)
    && SameVector(document["localBounds"]["center"], bounds.center));
Check("ordinary-dto-and-array", document["label"].Value<string>() == "evidence"
    && document["count"].Value<int>() == 3 && document["empty"].Type == JTokenType.Null
    && SameVector(document["values"][0], Vector3.zero)
    && SameVector(document["restVertex"], vector));
Check("null-root", WorkflowJson.Serialize(null, Formatting.None) == "null");

var previousCulture = CultureInfo.CurrentCulture;
try
{
    CultureInfo.CurrentCulture = CultureInfo.GetCultureInfo("fr-FR");
    Check("decimal-culture", SameVector(JObject.Parse(WorkflowJson.Serialize(vector)), vector));
}
finally { CultureInfo.CurrentCulture = previousCulture; }

var cycle = new Dictionary<string, object>();
cycle["self"] = cycle;
var rejected = false;
try { WorkflowJson.Serialize(cycle); }
catch (JsonSerializationException) { rejected = true; }
Check("unrelated-cycle-still-rejected", rejected);

var originalDefaults = JsonConvert.DefaultSettings;
Func<JsonSerializerSettings> sentinel = () => throw new InvalidOperationException("Global defaults consulted");
try
{
    JsonConvert.DefaultSettings = sentinel;
    Check("global-settings-isolation", SameVector(JObject.Parse(WorkflowJson.Serialize(vector)), vector)
        && ReferenceEquals(JsonConvert.DefaultSettings, sentinel));
}
finally { JsonConvert.DefaultSettings = originalDefaults; }

Console.WriteLine(JsonConvert.SerializeObject(new
{
    scope = "Actual helper and Unity/Newtonsoft value types under .NET 8, outside Unity. No workflow, mutation replay or speedup test.",
    passed, results
}, Formatting.Indented));
return passed ? 0 : 1;
