"""Observer surfaces a central refusal instead of an identity error; one workload RPC only."""
import asyncio
import os
from process_watch import process_started_ticks
from pathlib import Path
import tempfile
import types
import unittest

import native_workflow_observer as observer

WORKSPACE = str(observer.WORKSPACE)


class FakeClient:
    capabilities = {"workspace": WORKSPACE, "schema": "unity-workflow-mcp-v1", "service_pid": os.getpid(), "service_started_ticks": process_started_ticks()}
    outcome = None
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def call_tool(self, name, arguments, raise_on_error=True):
        FakeClient.calls.append(name)
        value = self.capabilities if name == "unity_workflow_capabilities" else FakeClient.outcome
        return types.SimpleNamespace(structured_content=value)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saved = observer.Client, observer.TRACE
        observer.Client, observer.TRACE = FakeClient, Path(self.temp.name) / "trace.jsonl"
        FakeClient.calls = []

    def tearDown(self):
        observer.Client, observer.TRACE = self.saved
        self.temp.cleanup()

    def run_with(self, outcome, action="workflow_run"):
        FakeClient.outcome = outcome
        params = {"action": action, "mode": "job", "submission_key": "k1", "manifest_path": "m.json"}
        return asyncio.run(observer.execute(params, 5))

    def test_rejection_reason_surfaced(self):
        reason = "Each check needs code_file and a string expect"
        response = self.run_with({"verified_success": False, "reason": reason, "submission_key": "k1",
                                  "recovery": "Query the same key if acceptance is uncertain"})
        self.assertFalse(response.success)
        self.assertEqual(reason, response.error)
        self.assertEqual(["unity_workflow_capabilities", "unity_job_run"], FakeClient.calls)

    def test_unknown_key_on_result_surfaced(self):
        response = self.run_with({"verified_success": False, "reason": "Unknown job", "submission_key": "k1"},
                                 action="workflow_result")
        self.assertEqual("Unknown job", response.error)
        self.assertEqual(["unity_workflow_capabilities", "unity_job_result"], FakeClient.calls)

    def test_mismatched_or_missing_key_still_identity_error(self):
        for outcome in ({"verified_success": False, "reason": "x", "submission_key": "other"},
                        {"verified_success": False, "reason": "x"},
                        {"verified_success": True, "submission_key": "k1"}):
            response = self.run_with(outcome)
            self.assertFalse(response.success)
            self.assertIn("identity missing/mismatched", response.error)

    def test_accepted_job_passes_through(self):
        response = self.run_with({"verified_success": True, "submission_key": "k1", "job_id": "j1", "state": "succeeded"})
        self.assertTrue(response.success)
        self.assertEqual("j1", response.data["job_id"])


if __name__ == "__main__":
    unittest.main()
