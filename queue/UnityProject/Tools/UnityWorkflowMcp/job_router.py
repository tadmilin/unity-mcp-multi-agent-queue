"""Route checked files/check jobs through the existing durable phase executor.

Legacy canonical manifests and ledgers remain untouched. Result recovery searches
both stores; a key collision is refused instead of guessing or replaying work.
"""
import json
import contextlib

from bridge import Bridge


class JobRouter(Bridge):
    def __init__(self, legacy, checked):
        super().__init__(root=legacy.root, home=legacy.home, worker_check=legacy.worker_check)
        self.legacy, self.checked = legacy, checked

    def locate(self, job_id=None, submission_key=None):
        found = []
        for bridge in (self.legacy, self.checked):
            try:
                snapshot = bridge.snapshot(job_id, submission_key)
                # Phase keys are visible only if they are file/check jobs.
                with contextlib.closing(bridge.connection()) as db:
                    kind = json.loads(db.execute('SELECT manifest FROM jobs WHERE id=?', (snapshot['id'],)).fetchone()[0])['kind']
                if bridge is self.legacy or kind in ('files', 'check'):
                    found.append((bridge, snapshot))
            except ValueError as error:
                if str(error) != 'Unknown job':
                    raise
        if len(found) > 1:
            raise ValueError('Ambiguous job key across ledgers; no replay')
        return found[0] if found else None

    def submit(self, submission_key, manifest_path):
        existing = self.locate(submission_key=submission_key)
        path = self.inside(manifest_path)
        if path.stat().st_size > 256 * 1024:
            raise ValueError('Manifest exceeds 256 KiB')
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(raw, dict) or raw.get('kind', 'files') not in ('files', 'check'):
            raise ValueError('job mode requires files/check')
        selected = self.checked if 'completion_steps' in raw else self.legacy
        if not existing and selected is self.legacy:
            raise ValueError('New jobs require completion_steps; import/queue success is not task completion')
        if existing and existing[0] is not selected:
            raise ValueError('Existing key cannot change executor or completion contract')
        return selected.submit(submission_key, manifest_path)

    def snapshot(self, job_id=None, submission_key=None, evidence=False):
        found = self.locate(job_id, submission_key)
        if not found:
            raise ValueError('Unknown job')
        bridge, snapshot = found
        return bridge.snapshot(snapshot['id'], evidence=evidence)

    async def wait(self, job_id, wait_seconds):
        found = self.locate(job_id=job_id)
        if not found:
            raise ValueError('Unknown job')
        value = await found[0].wait(job_id, wait_seconds)
        value['recovery'] = 'Use workflow_result mode=job with this same submission_key; never replay mutations'
        return value

    def capabilities(self):
        return {**self.legacy.capabilities(), 'job_completion_steps': True,
                'checked_quarantine_admission': 'durable-queued-v1',
                'checked_quarantine_observation': 'waits-within-original-call; cancellation-retains-job',
                'task_completion_requires': 'task_verified_success=true; null means unverified',
                'checked_job_executor': 'existing durable phase ledger and FIFO authority'}
