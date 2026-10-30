"""New checked work keeps a durable owner/executor while quarantine is fenced."""
import asyncio
import unittest
from test_unmarked_play_recovery import UnmarkedPlayRecoveryTests


class DurableQuarantineTests(unittest.TestCase):
    setUp=UnmarkedPlayRecoveryTests.setUp
    tearDown=UnmarkedPlayRecoveryTests.tearDown
    submit=UnmarkedPlayRecoveryTests.submit
    stranded=UnmarkedPlayRecoveryTests.stranded

    def test_admits_once_and_original_wait_finishes_after_owner_cleanup(self):
        worker,first=self.stranded()
        second=self.bridge.submit('second','phase.json')
        self.assertEqual(second,self.bridge.submit('second','phase.json'))
        self.assertEqual('queued',self.store.get(second)['state'])
        self.assertIsNone(self.store.next_job())
        async def run():
            pending=asyncio.create_task(self.bridge.wait(second,1))
            await asyncio.sleep(.03)
            self.assertFalse(pending.done(),'Quarantine is waiting, not observer termination')
            self.assertTrue(worker.recover_quarantined(first))
            self.assertEqual(second,self.store.next_job());worker.process(second)
            result=await pending
            self.assertTrue(result['verified_success'])
        asyncio.run(run())
        self.assertEqual(2,self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_zero_wait_reports_durable_blocker_without_cancellation(self):
        _,first=self.stranded();second=self.bridge.submit('second','phase.json')
        value=asyncio.run(self.bridge.wait(second,0))
        self.assertEqual(second,value['job_id'])
        self.assertEqual('queued',value['state'])
        self.assertEqual(first,value['blocked_by'][0]['job_id'])
        self.assertTrue(value['observation_timed_out'])
        self.assertFalse(value['job_cancelled_by_observer'])
        self.assertIsNone(self.store.get(second)['ticket'])

    def test_cancel_observation_keeps_job_and_foreign_rights(self):
        _,first=self.stranded();second=self.bridge.submit('second','phase.json')
        start=len(self.authority.calls)
        async def run():
            pending=asyncio.create_task(self.bridge.wait(second,1800))
            await asyncio.sleep(.03);pending.cancel()
            with self.assertRaises(asyncio.CancelledError):await pending
        asyncio.run(run())
        self.assertEqual('queued',self.store.get(second)['state'])
        self.assertEqual('quarantined',self.store.get(first)['state'])
        self.assertTrue(self.authority.owned)
        self.assertEqual(start,len(self.authority.calls))


if __name__=='__main__':unittest.main()
