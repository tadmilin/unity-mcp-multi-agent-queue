"""Shared authoring helpers for new checked jobs; never rewrites accepted jobs.

Submit these manifests using workflow_run mode=job. Completion code is authored
for the actual outcome; import or a callback-start acknowledgement is insufficient.
"""
import json
from pathlib import Path


def write_job(path, files, completion_steps, **options):
    path = Path(path)
    if not completion_steps:
        raise ValueError('Task completion needs at least one terminal check')
    if 'kind' in options or 'files' in options or 'completion_steps' in options:
        raise ValueError('Reserved job fields')
    value = dict(kind='files' if files else 'check', files=files,
                 completion_steps=completion_steps, **options)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
    return path


def script_value_check(path, type_name, member, expected):
    """Check the type and a public static field actually loaded by Unity."""
    code = ('var t=System.AppDomain.CurrentDomain.GetAssemblies().Select(a=>a.GetType('
            + json.dumps(type_name) + ')).FirstOrDefault(candidate=>candidate!=null); '
            'if(t==null) return "type-missing"; var f=t.GetField(' + json.dumps(member)
            + ',System.Reflection.BindingFlags.Public|System.Reflection.BindingFlags.Static); '
            'if(f==null) return "field-missing"; return Newtonsoft.Json.JsonConvert.SerializeObject(f.GetValue(null));')
    path = Path(path)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(code)
    return {'code_file': path.name, 'expect': json.dumps(expected, separators=(',', ':'))}
