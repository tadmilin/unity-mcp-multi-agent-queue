"""One local MCP session; independent log thread; one reload trigger; command-level recovery proof."""
import argparse
import datetime as dt
import json
from pathlib import Path
import threading
import time

from mcp_cli import Client
from observe_reload import extract_profile


def resource_value(response):
    return json.loads(response['result']['contents'][0]['text'])


def tool_value(response):
    result = response.get('result', {})
    if result.get('isError'):
        return {}
    if isinstance(result.get('structuredContent'), dict):
        return result['structuredContent']
    try:
        return json.loads(result['content'][0]['text'])
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def command_ready(response, expected_scene):
    value = tool_value(response)
    scenes = (value.get('data') or {}).get('scenes', [])
    return value.get('success') is True and any(
        s.get('path') == expected_scene and s.get('isLoaded') is True for s in scenes)


def watch_log(log, offset, stop, emit, shared):
    """No MCP dependency here. Timestamps are observation times, never inferred event times."""
    try:
        data = bytearray()
        tail = ''
        with log.open('rb') as stream:
            if log.stat().st_size < offset:
                raise RuntimeError('Log truncated before observation')
            stream.seek(offset)
            while not stop.is_set():
                if log.stat().st_size < stream.tell():
                    raise RuntimeError('Log rotated/truncated during observation')
                chunk = stream.read(65536)
                if not chunk:
                    stop.wait(0.2)
                    continue
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise RuntimeError('Log capture limit reached')
                tail += chunk.decode('utf-8', errors='replace')
                lines = tail.split('\n')
                tail = lines.pop()
                for line in lines:
                    if any(term in line for term in ('Begin MonoManager ReloadAssembly',
                        'Domain Reload Profiling:', 'Reloading assemblies', '[HTTP Reload]',
                        '[WebSocket]', 'Asset Pipeline Refresh', 'Failed to resume HTTP')):
                        # Exclude stack trace continuation lines.
                        if 'MoveNext' not in line and ' at ' not in line:
                            emit('log_marker', text=line[:1200])
                if shared.get('profile') is None:
                    profile = extract_profile(data.decode('utf-8', errors='replace'))
                    if profile:
                        shared['profile'] = profile
                        emit('native_reload_complete', profile=profile)
    except Exception as exc:
        shared['log_error'] = str(exc)
        emit('log_observer_error', error=str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=360)
    args = parser.parse_args()
    if not 10 <= args.timeout <= 600:
        parser.error('timeout must be 10..600 seconds')
    run = args.run_dir.resolve()
    run.relative_to(Path.cwd().resolve())
    run.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    events = run / 'events.jsonl'
    lock = threading.Lock()
    shared = {}
    stop = threading.Event()
    thread = None
    client = None
    summary = {'status': 'failed', 'command_success': False,
               'note': 'Arrival timestamps have polling/request uncertainty. Native reload completion alone does not prove Editor readiness.'}

    def emit(kind, **fields):
        event = {'kind': kind, 'observed_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                 'elapsed_s': round(time.monotonic()-started, 6), **fields}
        with lock, events.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event) + '\n')

    try:
        client = Client(timeout=35)
        instances = resource_value(client.rpc('resources/read', {'uri':'mcpforunity://instances'}))
        items = instances.get('instances', [])
        if len(items) != 1 or items[0].get('name') != 'tsonlinecopy':
            raise RuntimeError('Expected exactly the authorized project instance')
        initial_session = items[0]['session_id']
        emit('instances_before', value=instances)
        preflight = '''
if(EditorApplication.isPlaying || EditorApplication.isCompiling || EditorApplication.isUpdating)throw new System.Exception("Editor must be idle");
if(UnityEngine.Profiling.Profiler.enabled)throw new System.Exception("Profiler must be disabled");
for(int i=0;i<UnityEngine.SceneManagement.SceneManager.sceneCount;i++)if(UnityEngine.SceneManagement.SceneManager.GetSceneAt(i).isDirty)throw new System.Exception("Dirty scene");
return new {utc=System.DateTime.UtcNow.ToString("o"),pid=System.Diagnostics.Process.GetCurrentProcess().Id,logPath=Application.consoleLogPath,logOffset=new System.IO.FileInfo(Application.consoleLogPath).Length,scene=UnityEngine.SceneManagement.SceneManager.GetActiveScene().path,enabled=UnityEngine.Profiling.Profiler.enabled,enableBinaryLog=UnityEngine.Profiling.Profiler.enableBinaryLog,profileEditor=UnityEditorInternal.ProfilerDriver.profileEditor,logFile=UnityEngine.Profiling.Profiler.logFile,deepProfiling=UnityEditorInternal.ProfilerDriver.deepProfiling};'''
        response = client.rpc('tools/call', {'name':'execute_code','arguments':{'action':'execute','code':preflight}})
        emit('preflight_response', value=response)
        value = tool_value(response)
        if value.get('success') is not True:
            raise RuntimeError('No unambiguous preflight result; no reload requested')
        before = value['data']['result']
        (run/'before.json').write_text(json.dumps(before,indent=2),encoding='utf-8')
        log = Path(before['logPath']).resolve()
        log.relative_to(Path.cwd().resolve())
        thread = threading.Thread(target=watch_log, args=(log,before['logOffset'],stop,emit,shared), daemon=True)
        thread.start()
        # This is the only reload request. Do not retry it after an ambiguous tool response.
        emit('trigger_requested', trigger='EditorUtility.RequestScriptReload()', pid=before['pid'])
        try:
            response = client.rpc('tools/call', {'name':'execute_code','arguments':{'action':'execute','code':
                'EditorUtility.RequestScriptReload(); return "reload_requested";'}})
            emit('trigger_response', value=response)
        except Exception as exc:
            emit('trigger_transport_error', error=str(exc))
        deadline = time.monotonic() + args.timeout
        last_session = initial_session
        next_command = 0
        while time.monotonic() < deadline:
            try:
                instances = resource_value(client.rpc('resources/read', {'uri':'mcpforunity://instances'}))
                items = instances.get('instances', [])
                sessions = [x.get('session_id') for x in items]
                emit('instances_observed', count=len(items), sessions=sessions)
                new_session = next((x for x in items if x.get('id') == 'tsonlinecopy@c9a78098262cb018' and x.get('session_id') != initial_session), None)
                if new_session and new_session['session_id'] != last_session:
                    last_session = new_session['session_id']
                    emit('new_unity_session', session_id=last_session)
                if new_session and shared.get('profile') and time.monotonic() >= next_command:
                    emit('post_reload_command_started')
                    response = client.rpc('tools/call', {'name':'manage_scene','arguments':{'action':'get_loaded_scenes'}})
                    ready = command_ready(response, before['scene'])
                    emit('post_reload_command_result', success=ready, value=response)
                    if ready:
                        summary.update(status='recovered',command_success=True,session_before=initial_session,
                                       session_after=last_session,scene_result=tool_value(response))
                        break
                    next_command = time.monotonic() + 35
            except Exception as exc:
                emit('probe_error', error=str(exc))
                next_command = time.monotonic() + 35
            stop.wait(2)
        else:
            summary['status'] = 'observation_timeout'
    except Exception as exc:
        summary['error'] = str(exc)
        emit('fatal_error', error=str(exc))
    finally:
        stop.set()
        if thread:
            thread.join(timeout=3)
        if client:
            client.close()
        summary.update(profile=shared.get('profile'),log_error=shared.get('log_error'),
                       observer_stopped=thread is None or not thread.is_alive(),elapsed_s=time.monotonic()-started)
        (run/'result.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in summary.items() if k not in ('profile','scene_result')}),flush=True)
    return 0 if summary['command_success'] and summary['observer_stopped'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
