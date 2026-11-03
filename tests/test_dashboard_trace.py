from __future__ import annotations

import unittest

from super_agent.supervisor.workflow import SharedTaskState, WorkflowRequest, WorkflowStatus
from super_agent.supervisor.telemetry import TraceStore, WorkflowAlreadyExists


class DashboardTraceTests(unittest.TestCase):
    def test_trace_redacts_nested_secrets_and_returns_independent_snapshots(self) -> None:
        store = TraceStore()
        store.begin(WorkflowRequest(global_task_id="T1", goal="Plan", part_id="Part_A", metadata={"api_key": "secret"}))
        store.emit("T1", "http_response", data={"password": "hidden", "nested": [{"access_token": "hidden", "part_id": "Part_A"}]})
        snapshot = store.snapshot()
        self.assertEqual(snapshot["workflows"][0]["request"]["metadata"]["api_key"], "[redacted]")
        self.assertEqual(snapshot["events"][-1]["data"]["nested"][0]["access_token"], "[redacted]")
        snapshot["events"][-1]["data"]["nested"][0]["part_id"] = "Changed"
        self.assertEqual(store.snapshot()["events"][-1]["data"]["nested"][0]["part_id"], "Part_A")

    def test_retention_never_evicts_active_workflow_and_cursors_are_monotonic(self) -> None:
        store = TraceStore(max_workflows=1, max_events=2)
        request = WorkflowRequest(global_task_id="T1", goal="Plan", part_id="Part_A")
        store.begin(request)
        with self.assertRaises(WorkflowAlreadyExists):
            store.begin(request)
        with self.assertRaisesRegex(ValueError, "Too many active"):
            store.begin(request.model_copy(update={"global_task_id": "T2"}))
        store.finish(SharedTaskState(global_task_id="T1", user_goal="Plan", status=WorkflowStatus.COMPLETED))
        store.begin(request.model_copy(update={"global_task_id": "T2"}))
        self.assertEqual(list(store.workflows), ["T2"])
        self.assertEqual([event["id"] for event in store.events], [2, 3])
        self.assertEqual(store.events_since(2)[0]["global_task_id"], "T2")


if __name__ == "__main__":
    unittest.main()
