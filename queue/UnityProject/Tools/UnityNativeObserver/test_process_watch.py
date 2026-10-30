"""Real process-exit notifications; the child is the test target, not a Unity waiter."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from process_watch import ProcessWatch, ServiceExited, guarded_workload, process_started_ticks


class Client:
    def __init__(self, immediate=False):
        self.immediate = immediate
        self.calls = 0
        self.started = asyncio.Event()
        self.cancelled = False

    async def call_tool(self, *args, **kwargs):
        self.calls += 1
        self.started.set()
        if self.immediate:
            return 'terminal'
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class WatchTests(unittest.IsolatedAsyncioTestCase):
    def capabilities(self):
        return {'service_pid': os.getpid(), 'service_started_ticks': process_started_ticks()}

    async def test_success_and_watch_cleanup(self):
        client = Client(immediate=True)
        self.assertEqual(await guarded_workload(client, 'run', {}, self.capabilities()), 'terminal')
        self.assertEqual(client.calls, 1)

    async def test_cancel_cleans_thread_and_request(self):
        client = Client()
        task = asyncio.create_task(guarded_workload(client, 'run', {}, self.capabilities()))
        await client.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertTrue(client.cancelled)
        self.assertEqual(client.calls, 1)

    async def test_real_process_exit_ends_original_request_without_replay(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            caps = {'service_pid': child.pid, 'service_started_ticks': process_started_ticks(child.pid)}
            client = Client()
            task = asyncio.create_task(guarded_workload(client, 'run', {}, caps))
            await client.started.wait()
            child.stdin.close()
            with self.assertRaises(ServiceExited):
                await asyncio.wait_for(task, 2)
            self.assertEqual(client.calls, 1)
            self.assertTrue(client.cancelled)
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=2)

    async def test_wrong_lifetime_never_dispatches(self):
        client = Client()
        caps = self.capabilities()
        caps['service_started_ticks'] += 1
        with self.assertRaises(ServiceExited):
            await guarded_workload(client, 'run', {}, caps)
        self.assertEqual(client.calls, 0)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(WatchTests))
    with Path(sys.argv[1]).open('x', encoding='utf-8') as stream:
        json.dump({'passed': result.wasSuccessful(), 'tests': result.testsRun,
            'scope': 'Actual Windows child process exit; no Unity acceptance',
            'failures': [str(x) for x in result.failures], 'errors': [str(x) for x in result.errors]}, stream, indent=2)
    raise SystemExit(not result.wasSuccessful())
