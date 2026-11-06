"""Read-only source-variation census; timings are whole RPC durations, not compile time."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('database', type=Path)
args = parser.parse_args()
db = args.database.resolve()
before = hashlib.sha256(db.read_bytes()).hexdigest()
rows = defaultdict(list)
pending = {}
boot = None
with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as connection:
    connection.execute('PRAGMA query_only=ON')
    for seq, job, timestamp, kind, detail in connection.execute(
            'SELECT seq,job,time,kind,detail FROM events ORDER BY seq'):
        data = json.loads(detail)
        if kind == 'status':
            boot = data.get('bootUtc', boot)
        if kind == 'intent' and data.get('action') == 'execute':
            label = data['operation'][len(job) + 1:]
            if label in ('start-play', 'stop-owned-play'):
                value = dict(boot=boot, code=data['parameters']['code'])
                rows[label].append(value)
                pending[job] = (timestamp, value)
        if kind == 'response' and data.get('action') == 'execute' and job in pending:
            started, value = pending.pop(job)
            value['seconds'] = timestamp - started
after = hashlib.sha256(db.read_bytes()).hexdigest()
assert before == after, 'Database changed while inspected'
summary = {}
for label, values in rows.items():
    durations = [v['seconds'] for v in values if 'seconds' in v]
    summary[label] = dict(calls=len(values), unique_sources=len({v['code'] for v in values}),
        observed_boots=len({v['boot'] for v in values}), matched_responses=len(durations),
        rpc_seconds_sum=round(sum(durations), 3), rpc_seconds_median=round(statistics.median(durations), 3))
print(json.dumps(dict(database_sha256=before, summary=summary,
    limitations=['Boot timestamps observed at status samples are not a full reload census.',
                 'RPC durations include transport, compilation and Unity work; no predicted speedup.',
                 'This is one historical queue run, not a matched performance trial.']), indent=2))
