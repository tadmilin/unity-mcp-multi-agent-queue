"""Read preserved SQLite only; emit sanitized event brackets, never credentials."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('database', type=Path)
p.add_argument('--output', type=Path, required=True)
args = p.parse_args()
if args.output.exists():
    p.error('Output already exists; choose a fresh path')
db = args.database.resolve()
checksum = hashlib.sha256(db.read_bytes()).hexdigest()
with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as c:
    c.execute('PRAGMA query_only=ON')
    events = [(s, j, t, k, json.loads(d)) for s, j, t, k, d in c.execute(
        'SELECT seq,job,time,kind,detail FROM events ORDER BY seq')]
    states = dict(c.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
counts = Counter(e[3] for e in events)
commands = []
imports = []
for seq, job, recovered_at, kind, detail in events:
    if kind == 'terminal_receipt_recovered':
        own = [e for e in events if e[1] == job and e[0] <= seq]
        intent = next(e for e in own if e[3] == 'intent' and e[4].get('operation') == detail['operation'])
        response = next(e for e in own if e[0] > intent[0] and e[3] == 'response' and e[4].get('action') == 'execute')
        initial = next(e for e in own if e[3] == 'initial_readiness_armed')
        commands.append(dict(label='preflight',
            execute_intent_to_timeout_response_seconds=round(response[2]-intent[2],6),
            timeout_response_to_recovered_receipt_seconds=round(recovered_at-response[2],6),
            initial_readiness_armed_to_recovered_receipt_seconds=round(recovered_at-initial[2],6),
            execute_intent_to_recovered_receipt_seconds=round(recovered_at-intent[2],6),
            unavailable_status_events=sum(e[3]=='status_unavailable' for e in own),
            matching_execute_intents=sum(e[3]=='intent' and e[4].get('operation')==detail['operation'] for e in own)))
    if kind == 'import_confirmation_unavailable':
        own = [e for e in events if e[1] == job]
        confirmed = next(e for e in own if e[0] > seq and e[3] == 'import_wait_confirmed')
        armed = next(e for e in own if e[3] == 'import_wait_armed')
        second = next(e for e in own if e[0] > seq and e[3] == 'import_confirmation_intent')
        imports.append(dict(sample=len(imports)+1,version=armed[4]['version'],
            unavailable_to_confirmed_seconds=round(confirmed[2]-recovered_at,6),
            unavailable_to_next_confirmation_intent_seconds=round(second[2]-recovered_at,6),
            armed_to_confirmed_seconds=round(confirmed[2]-armed[2],6),
            confirmation_intents=sum(e[3]=='import_confirmation_intent' for e in own),
            status_rpc_count=confirmed[4]['status_rpc_count'],
            notified_session_changed=[e[4].get('session_changed') for e in own if e[3]=='import_wait_notified'],
            import_mutation_intents=sum(e[3]=='intent' and e[4].get('action')=='import' for e in own)))
durations = [x['unavailable_to_confirmed_seconds'] for x in imports]
result = dict(database_sha256=checksum,job_states=states,
    event_counts={k: counts[k] for k in ('terminal_receipt_recovered','status_unavailable',
        'import_confirmation_unavailable','import_confirmation_wait_reconnect','import_cleanup_recovered',
        'cleanup_recovered','unmarked_cleanup_recovered','transport_error')},
    terminal_receipt_recovery=commands,import_reconfirmation=imports,
    import_unavailable_to_confirmed_summary_seconds=dict(n=len(durations),minimum=min(durations),
        median=statistics.median(durations),maximum=max(durations)),
    limitations=['Historical run used v3; checkout initially used v2 and was subsequently ported byte-for-byte to historical v3. Timings remain historical, not new-install measurements.',
        'Event brackets include transport and scheduling; not pure recovery overhead or guaranteed deadlines.',
        'No cleanup-recovered events in this ledger: late-Stop/import/unmarked cleanup coverage is offline only in this audit.'])
assert hashlib.sha256(db.read_bytes()).hexdigest() == checksum
args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result['import_unavailable_to_confirmed_summary_seconds']))
