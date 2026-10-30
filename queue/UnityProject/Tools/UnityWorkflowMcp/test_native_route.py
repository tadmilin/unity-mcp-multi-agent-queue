"""Exercise the actual HTTP ingress contract with an isolated file ledger."""
import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import unittest

from server import create_server
from bridge import Bridge, coordinator
import httpx


class NativeRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent / '.scratch')
        self.root = Path(self.temp.name).resolve()
        self.bridge = Bridge(self.root, worker_check=lambda: True, interval=.01)
        with contextlib.closing(coordinator.Store(self.bridge.home)) as store:
            store.control('stop', '0')
        (self.bridge.home / 'installed.json').write_text('{}')
        (self.root / 'check.json').write_text('{"kind":"check"}')
        self.server = create_server(self.bridge)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.http_app()), base_url='http://localhost')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def send(self, action, **extra):
        return (await self.client.post('/workflow', json={'action': action, 'mode': 'job', 'submission_key': 'native-key', **extra})).json()

    async def test_native_run_and_poll_reuse_same_durable_job(self):
        self.bridge.submit('native-key', 'check.json')
        with contextlib.closing(coordinator.Store(self.root / 'JobQueue/phases')):
            pass
        first = await self.send('workflow_run', manifest_path='check.json')
        job = first['data']['job_id']
        self.assertFalse(first['data']['completed'])
        for action in ('workflow_poll', 'workflow_result', 'workflow_run'):
            reply = await self.send(action, manifest_path='check.json')
            self.assertEqual(job, reply['data']['job_id'])
        with contextlib.closing(coordinator.Store(self.bridge.home)) as store:
            self.assertEqual(1, store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])
            store.update(job, state='succeeded', result=coordinator.encode({'console': {'success': True, 'data': []}}))
            store.event(job, 'response', {'action': 'release', 'response': {'success': True}})
        self.assertTrue((await self.send('workflow_poll'))['data']['verified_success'])

    async def test_native_unknown_result_does_not_submit(self):
        self.assertFalse((await self.send('workflow_result'))['success'])
        with contextlib.closing(self.bridge.connection()) as connection:
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0])

    async def test_native_capabilities_do_not_start_phase_worker(self):
        reply = await self.send('workflow_capabilities')
        self.assertTrue(reply['success'])
        self.assertFalse(reply['data']['phase_worker_running'])
        self.assertFalse((self.root / 'JobQueue/phases').exists())

    async def test_native_blocks_browser_origin(self):
        reply = await self.client.post('/workflow', json={'action': 'workflow_capabilities'}, headers={'Origin': 'https://example.invalid'})
        self.assertEqual(403, reply.status_code)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(NativeRouteTests))
    args.output.write_text(json.dumps({'passed': result.wasSuccessful(), 'tests': result.testsRun,
        'failures': [str(x) for x in result.failures], 'errors': [str(x) for x in result.errors]}, indent=2))
    raise SystemExit(not result.wasSuccessful())
