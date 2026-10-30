"""Isolated durable-queue and real SDK tests; no production database or Unity."""
import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge import Bridge, coordinator


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratch = Path(__file__).parent / '.scratch'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'JobQueue'
        with contextlib.closing(coordinator.Store(self.home)) as store:
            store.control('stop', '0')
        (self.home / 'installed.json').write_text('{}')
        (self.root / 'staging').mkdir()
        (self.root / 'staging/check.json').write_text('{"kind":"check"}')
        self.bridge = Bridge(self.root, self.home, worker_check=lambda: True, interval=.01)

    def tearDown(self):
        self.temp.cleanup()

    def change(self, job, state, console=None):
        console = console if console is not None else {'success': True, 'data': []}
        with contextlib.closing(coordinator.Store(self.home)) as store:
            store.update(job, state=state, phase='done' if state in ('succeeded','quarantined','failed') else 'import',
                         result=coordinator.encode({'console': console}))
            if state == 'succeeded':
                store.event(job, 'response', {'action':'release','response':{'success':True}})

    def count(self):
        with contextlib.closing(self.bridge.connection()) as connection:
            return connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]

    async def test_one_call_waits_through_running_without_subprocess(self):
        job = self.bridge.submit('one', 'staging/check.json')
        async def worker():
            await asyncio.sleep(.04)
            self.change(job, 'running')
            await asyncio.sleep(.04)
            self.change(job, 'succeeded')
        with patch('subprocess.Popen', side_effect=AssertionError('observer spawned process')):
            result, _ = await asyncio.gather(self.bridge.run('one', 'staging/check.json', 2), worker())
        self.assertTrue(result['verified_success'])
        self.assertEqual(1, self.count())

    async def test_timeout_keeps_durable_job_and_same_key(self):
        result = await self.bridge.run('time', 'staging/check.json', .03)
        self.assertTrue(result['observation_timed_out'])
        self.assertFalse(result['verified_success'])
        self.assertEqual('queued', self.bridge.snapshot(result['job_id'])['state'])
        self.assertEqual(result['job_id'], self.bridge.submit('time', 'staging/check.json'))

    async def test_cancelled_wait_does_not_cancel_job(self):
        job = self.bridge.submit('cancel', 'staging/check.json')
        task = asyncio.create_task(self.bridge.wait(job, 10))
        await asyncio.sleep(.03)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual('queued', self.bridge.snapshot(job)['state'])
        self.change(job, 'succeeded')
        self.assertTrue((await self.bridge.run('cancel', 'staging/check.json', 1))['verified_success'])

    async def test_concurrent_same_key_accepts_exactly_one_job(self):
        results = await asyncio.gather(*[self.bridge.run('same', 'staging/check.json', 0) for _ in range(4)])
        self.assertEqual(1, len({r['job_id'] for r in results}))
        self.assertEqual(1, self.count())

    async def test_changed_key_content_rejected(self):
        self.bridge.submit('same', 'staging/check.json')
        (self.root / 'staging/check.json').write_text('{"kind":"check","depends_on":["missing"]}')
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.bridge.submit('same', 'staging/check.json')
        self.assertEqual(1, self.count())

    async def test_console_errors_are_not_verified_success(self):
        job = self.bridge.submit('errors', 'staging/check.json')
        self.change(job, 'succeeded', {'success':True,'data':[{'message':'existing error'}]})
        result = await self.bridge.wait(job, 1)
        self.assertFalse(result['verified_success'])
        self.assertEqual('console_not_verified', result['reason'])
        self.assertEqual('succeeded', result['state'])

    async def test_unparseable_console_not_verified(self):
        job = self.bridge.submit('shape', 'staging/check.json')
        for console in ({'success':True,'data':None},{'success':True,'data':{}},{'success':False,'data':[]}):
            self.change(job, 'succeeded', console)
            self.assertFalse((await self.bridge.wait(job, 0))['verified_success'])

    async def test_quarantine_is_attention_not_success(self):
        job = self.bridge.submit('q', 'staging/check.json')
        self.change(job, 'quarantined')
        result = await self.bridge.wait(job, 3)
        self.assertEqual('quarantined', result['reason'])
        self.assertFalse(result['verified_success'])
        self.assertFalse(result['completed'])
        self.assertTrue(result['attention_required'])
        with self.assertRaisesRegex(ValueError, 'quarantine'):
            self.bridge.submit('new', 'staging/check.json')

    async def test_failed_and_unknown_states_fail_closed(self):
        job = self.bridge.submit('states', 'staging/check.json')
        for state in ('failed','cancelled','abandoned','unexpected'):
            self.change(job, state)
            result = await self.bridge.wait(job, 3)
            self.assertFalse(result['verified_success'])
            self.assertFalse(result['observation_timed_out'])

    async def test_missing_dependency_rejected_before_acceptance(self):
        (self.root / 'staging/check.json').write_text('{"kind":"check","depends_on":["missing"]}')
        with self.assertRaisesRegex(ValueError, 'Unknown dependency'):
            self.bridge.submit('bad', 'staging/check.json')
        self.assertEqual(0, self.count())

    async def test_stopped_worker_rejects_new_but_returns_existing(self):
        job = self.bridge.submit('old', 'staging/check.json')
        self.bridge.worker_check = lambda: False
        with self.assertRaisesRegex(ValueError, 'not running'):
            self.bridge.submit('new', 'staging/check.json')
        self.assertEqual(job, self.bridge.submit('old', 'staging/check.json'))
        self.assertEqual(1, self.count())

    async def test_stopping_worker_rejects_before_acceptance(self):
        with contextlib.closing(coordinator.Store(self.home)) as store:
            store.control('stop', '1')
        with self.assertRaisesRegex(ValueError, 'stopping'):
            self.bridge.submit('new', 'staging/check.json')
        self.assertEqual(0, self.count())

    async def test_invalid_wait_never_submits(self):
        for budget in (-1,1801,float('nan'),True,'10'):
            with self.assertRaises(ValueError):
                await self.bridge.run('bad','staging/check.json',budget)
        self.assertEqual(0, self.count())

    async def test_staging_paths_cannot_escape(self):
        with self.assertRaises(ValueError):
            self.bridge.submit('bad','../elsewhere.json')
        (self.root / 'staging/bad.json').write_text(json.dumps({'files':[{'path':'Assets/Example.cs',
            'source':'../../secret.txt','expectedSha256':'missing'}]}))
        with self.assertRaises(ValueError):
            self.bridge.submit('bad','staging/bad.json')
        self.assertEqual(0,self.count())

    async def test_file_bytes_are_canonical_snapshot_and_staging_clean(self):
        (self.root / 'staging/Example.cs.txt').write_text('class Example {}')
        (self.root / 'staging/files.json').write_text(json.dumps({'files':[{'path':'Assets/Editor/Example.cs',
            'source':'Example.cs.txt','expectedSha256':'missing'}]}))
        job = self.bridge.submit('files','staging/files.json')
        with contextlib.closing(coordinator.Store(self.home)) as store:
            payload=json.loads(store.get(job)['manifest'])
        self.assertEqual('Assets/Editor/Example.cs',payload['files'][0]['path'])
        self.assertFalse(list((self.home/'mcp-staging').iterdir()))

    async def test_public_results_exclude_owner_credentials(self):
        job=self.bridge.submit('secret','staging/check.json')
        with contextlib.closing(coordinator.Store(self.home)) as store:
            store.update(job,token='private-owner-token',result=coordinator.encode({'token':'nested-secret'}))
        snapshot=self.bridge.snapshot(submission_key='secret',evidence=True)
        self.assertNotIn('private-owner-token',json.dumps(snapshot))
        self.assertNotIn('nested-secret',json.dumps(snapshot))

    async def test_large_observation_history_is_not_returned(self):
        job=self.bridge.submit('history','staging/check.json')
        with contextlib.closing(coordinator.Store(self.home)) as store:
            for _ in range(80): store.event(job,'status',{'sample':'x'*2000})
        self.change(job,'succeeded')
        result=await self.bridge.wait(job,1)
        self.assertTrue(result['verified_success'])
        self.assertEqual(80,result['snapshot']['evidence']['event_counts']['status'])
        self.assertLess(len(json.dumps(result)),4000)

    async def test_missing_release_receipt_is_not_success(self):
        job=self.bridge.submit('receipt','staging/check.json')
        with contextlib.closing(coordinator.Store(self.home)) as store:
            store.update(job,state='succeeded',result=coordinator.encode({'console':{'success':True,'data':[]}}))
        result=await self.bridge.wait(job,1)
        self.assertFalse(result['verified_success'])
        self.assertEqual('release_not_verified',result['reason'])

    async def test_real_sdk_registration_and_error_mapping(self):
        from server import create_server
        from fastmcp import Client
        # Recovery of an existing legacy key remains supported. New unchecked
        # submissions are rejected by JobRouter and tested separately below.
        self.bridge.submit('sdk', 'staging/check.json')
        # Production service startup provisions the second ledger. Provision it
        # directly in this fixture without launching its worker.
        with contextlib.closing(coordinator.Store(self.root / 'JobQueue/phases')):
            pass
        async with Client(create_server(self.bridge)) as client:
            names={t.name for t in await client.list_tools()}
            self.assertEqual({'unity_job_run','unity_job_result','unity_workflow_capabilities',
                              'unity_phase_run','unity_phase_result'},names)
            result=await client.call_tool('unity_job_run',{'submission_key':'sdk',
                'manifest_path':'staging/check.json','wait_seconds':0},raise_on_error=False)
            self.assertTrue(result.is_error)
            data=result.structured_content
            self.change(data['job_id'],'succeeded')
            resumed=await client.call_tool('unity_job_result',{'submission_key':'sdk','wait_seconds':1})
            self.assertTrue(resumed.structured_content['verified_success'])
            self.assertEqual(1,self.count())

    async def test_sdk_rejects_new_unchecked_job_without_admission(self):
        from server import create_server
        from fastmcp import Client
        with contextlib.closing(coordinator.Store(self.root / 'JobQueue/phases')):
            pass
        async with Client(create_server(self.bridge)) as client:
            result = await client.call_tool('unity_job_run', {
                'submission_key': 'unchecked-new', 'manifest_path': 'staging/check.json',
                'wait_seconds': 0}, raise_on_error=False)
            self.assertTrue(result.is_error)
            self.assertIn('completion_steps', result.structured_content['reason'])
            self.assertEqual(0, self.count())


if __name__=='__main__':
    import argparse,time
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): parser.error('Use a new evidence filename')
    started=time.monotonic()
    result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(BridgeTests))
    report={'test_count':result.testsRun,'passed':result.wasSuccessful(),
        'failures':[(str(t),trace) for t,trace in result.failures],
        'errors':[(str(t),trace) for t,trace in result.errors],
        'elapsed_seconds':time.monotonic()-started,
        'scope':'temporary DB and real FastMCP in-process registry; no production queue or Unity'}
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
