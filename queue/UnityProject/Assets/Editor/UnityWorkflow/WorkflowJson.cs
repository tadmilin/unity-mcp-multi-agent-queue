using System;
using System.Globalization;
using System.IO;
using Newtonsoft.Json;
using UnityEngine;

namespace UnityWorkflow
{
    /// <summary>
    /// Writes workflow evidence DTOs with explicit Vector3 and Bounds values.
    /// Opt in at the evidence-writing call. This does not change global JSON
    /// settings, execute a workflow, or serialize Unity object graphs.
    /// </summary>
    public static class WorkflowJson
    {
        public static string Serialize(object value, Formatting formatting = Formatting.Indented)
        {
            // Create rather than CreateDefault: evidence serialization must not
            // inherit application-wide converters or change their settings.
            var serializer = JsonSerializer.Create(new JsonSerializerSettings
            {
                Formatting = formatting,
                Converters = { new UnityValueConverter() }
            });
            using (var output = new StringWriter(CultureInfo.InvariantCulture))
            using (var writer = new JsonTextWriter(output))
            {
                serializer.Serialize(writer, value);
                writer.Flush();
                return output.ToString();
            }
        }

        private sealed class UnityValueConverter : JsonConverter
        {
            public override bool CanRead => false;

            public override bool CanConvert(Type type)
            {
                return type == typeof(Vector3) || type == typeof(Bounds);
            }

            public override void WriteJson(JsonWriter writer, object value, JsonSerializer serializer)
            {
                writer.WriteStartObject();
                if (value is Vector3 vector)
                {
                    writer.WritePropertyName("x");
                    writer.WriteValue(vector.x);
                    writer.WritePropertyName("y");
                    writer.WriteValue(vector.y);
                    writer.WritePropertyName("z");
                    writer.WriteValue(vector.z);
                }
                else if (value is Bounds bounds)
                {
                    writer.WritePropertyName("center");
                    serializer.Serialize(writer, bounds.center);
                    writer.WritePropertyName("size");
                    serializer.Serialize(writer, bounds.size);
                }
                else
                {
                    throw new JsonSerializationException("Unsupported workflow value type.");
                }
                writer.WriteEndObject();
            }

            public override object ReadJson(JsonReader reader, Type type, object existingValue,
                JsonSerializer serializer)
            {
                throw new NotSupportedException("WorkflowJson writes evidence only.");
            }
        }
    }
}
