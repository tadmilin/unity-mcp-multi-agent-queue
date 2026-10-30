"""Safety tests at the worker/notification seam. Not Unity acceptance."""
import copy
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from claim_wait import ClaimWait, ClaimWaitError, VERSION
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'UnityNativeObserver'))
from import_notifications import write_snapshot


class ClaimWaitTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.gate = self.root / 'Library/EditorQueueGate/state.json'
        self.ready = self.root / 'Library/UnityWorkflowSignals/editor-readiness.json'
        self.home = self.root / 'JobQueue/phases'
        for folder in (self.gate.parent, self.ready.parent, self.home):
            folder.mkdir(parents=True, exist_ok=True)
        self.calls, self.events, self.finishes = [], [], []
        self.stop = '0'
        self.state = {'version': 1, 'waiting': [{'ticket': 2, 'owner': 'coordinator-job',
            'secretHash': sha256(b'secret').hexdigest(), 'deadline': time.time()+10}], 'holder': None}
        self.signal = {'schema': VERSION, 'workspace': str(self.root), 'pid': 22,
                       'epoch': 'boot', 'sequence': 1, 'idle': True}
        self.reply = {'success': True, 'data': {'ticket': 2, 'owner': 'coordinator-job'}}
        self.live = None
        def rpc(job, action, **kwargs):
            self.calls.append(action)
            if action == 'claim':
                if isinstance(self.reply, Exception):
                    raise self.reply
                return self.reply
            return {'success': True}
        def observe(action, **kwargs):
            self.calls.append(action)
            return self.live
        def require(response):
            if response.get('success') is not True:
                raise RuntimeError('refused')
        self.worker = SimpleNamespace(root=self.root, timeout=.3, safe=True,
            client=SimpleNamespace(token='secret', client=SimpleNamespace(timeout=30), call=observe),
            store=SimpleNamespace(home=self.home, get=lambda job: {'ticket': 2},
                control=lambda key: self.stop, event=lambda *args: self.events.append(args)),
            rpc=rpc, require=require, finish=lambda *args: self.finishes.append(args),
            has_error=lambda r,c: isinstance(r,dict) and c in (r.get('error'),r.get('message')))
        self.publish()

    def publish(self):
        write_snapshot(self.gate, self.state)
        write_snapshot(self.ready, self.signal)

    def run_wait(self, callback=None, delay=.04):
        observation = ClaimWait(self.worker, 'job', {'editor': {'pid':22}})
        self.addCleanup(observation.changes.close)
        if callback:
            timer = threading.Timer(delay, callback)
            timer.start()
            self.addCleanup(timer.join)
        return observation.run()

    def test_holder_wait_no_rpc_until_real_directory_change(self):
        self.state['holder'] = {'ticket':1}
        self.publish()
        def released():
            self.assertEqual(self.calls, [])
            self.state['holder'] = None
            self.publish()
        self.assertTrue(self.run_wait(released))
        self.assertEqual(self.calls, ['claim'])

    def test_fifo_waits_for_earlier_ticket(self):
        self.state['waiting'].insert(0, {'ticket':1, 'deadline':time.time()+3})
        self.publish()
        def preceding_done():
            self.assertEqual(self.calls, [])
            self.state['waiting'].pop(0)
            self.publish()
        self.assertTrue(self.run_wait(preceding_done))
        self.assertEqual(self.calls, ['claim'])

    def test_expired_predecessor_wakes_authority_without_file_change(self):
        self.state['waiting'].insert(0, {'ticket':1, 'deadline':time.time()+.04})
        self.publish()
        self.assertTrue(self.run_wait())
        self.assertEqual(self.calls, ['claim'])

    def test_busy_readiness_waits_for_transition(self):
        self.signal['idle'] = False
        self.publish()
        def became_ready():
            self.assertEqual(self.calls, [])
            self.signal.update(idle=True, sequence=2)
            self.publish()
        self.assertTrue(self.run_wait(became_ready))
        self.assertEqual(self.calls, ['claim'])

    def test_deadline_cancels_only_own_waiter_no_claim(self):
        self.worker.timeout=.04
        self.state['holder']={'ticket':1}
        self.publish()
        self.assertFalse(self.run_wait())
        self.assertEqual(self.calls, ['cancel'])
        self.assertEqual(self.finishes[0][1], 'cancelled')

    def test_ticket_lease_caps_timeout(self):
        self.state['holder']={'ticket':1}
        self.state['waiting'][0]['deadline']=time.time()+.04
        self.publish()
        start=time.monotonic()
        self.assertFalse(self.run_wait())
        self.assertLess(time.monotonic()-start,.25)
        self.assertEqual(self.calls, ['cancel'])

    def test_stop_wakes_from_local_ledger_change(self):
        self.state['holder']={'ticket':1}
        self.publish()
        def stopped():
            self.stop='1'
            (self.home/'control-event').write_text('stop')
        self.assertFalse(self.run_wait(stopped))
        self.assertEqual(self.calls, ['cancel'])

    def test_stale_ready_denial_is_not_repeated_without_transition(self):
        self.reply={'success':False,'error':'EDITOR_QUEUE_EDITOR_NOT_IDLE'}
        self.worker.timeout=.3
        self.assertFalse(self.run_wait())
        self.assertEqual(self.calls, ['claim','cancel'])

    def test_workspace_mismatch_never_claims(self):
        self.signal['workspace']=str(self.root/'other')
        self.publish()
        self.assertFalse(self.run_wait())
        self.assertEqual(self.calls, ['cancel'])
        self.assertEqual(self.finishes[0][1], 'failed')

    def test_foreign_pid_hint_defers_to_one_authority_claim(self):
        self.signal['pid']=99
        self.publish()
        self.assertTrue(self.run_wait())
        self.assertEqual(self.calls, ['claim'])
        self.assertEqual([e[1] for e in self.events].count('claim_wait_foreign_hint'), 1)

    def test_foreign_pid_rewrites_never_poll_authority(self):
        self.signal['pid']=99
        self.reply={'success':False,'error':'EDITOR_QUEUE_EDITOR_NOT_IDLE'}
        self.publish()
        def import_worker_rewrites():
            for sequence in range(2, 7):
                self.signal['sequence']=sequence
                write_snapshot(self.ready, self.signal)
                time.sleep(.02)
        self.assertFalse(self.run_wait(import_worker_rewrites))
        self.assertEqual(self.calls, ['claim','cancel'])
        self.assertEqual(self.finishes[0][1], 'cancelled')

    def test_foreign_pid_then_editor_transition_claims(self):
        self.signal['pid']=99
        self.reply={'success':False,'error':'EDITOR_QUEUE_EDITOR_NOT_IDLE'}
        self.publish()
        def editor_republished():
            self.assertEqual(self.calls, ['claim'])
            self.reply={'success':True,'data':{'ticket':2,'owner':'coordinator-job'}}
            self.signal.update(pid=22, sequence=2, idle=True)
            write_snapshot(self.ready, self.signal)
        self.assertTrue(self.run_wait(editor_republished, delay=.08))
        self.assertEqual(self.calls, ['claim','claim'])

    def test_lost_claim_one_observation_no_replay_or_cancel(self):
        self.reply=OSError('lost')
        self.live={'success':False}
        with self.assertRaises(ClaimWaitError):
            self.run_wait()
        self.assertFalse(self.worker.safe)
        self.assertEqual(self.calls, ['claim','status'])
        self.assertEqual(self.finishes, [])

    def test_lost_claim_requires_matching_unused_owner(self):
        self.reply=OSError('lost')
        self.live={'success':True,'data':{'holder':{'ticket':2,'owner':'coordinator-job'}}}
        self.assertTrue(self.run_wait())
        self.assertEqual(self.calls, ['claim','status'])
        self.assertEqual(self.worker.client.client.timeout,30)


if __name__ == '__main__':
    unittest.main()
