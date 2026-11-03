from __future__ import annotations

import unittest

from super_agent.supervisor.workflow import WorkflowRequest
from skku.models import RobotPlanArtifact, RobotPath, RobotTarget, RobotValidation, McpToolResult
from super_agent.supervisor.telemetry import TraceStore
from super_agent.supervisor.unity_preview import UnityPreviewStore, UnityPreviewError


class UnityPreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.traces = TraceStore()
        self.traces.begin(WorkflowRequest(global_task_id="T", goal="Plan", part_id="P"))
        self.store = UnityPreviewStore(self.traces, timeout_seconds=.01)
        plan = RobotPlanArtifact(global_task_id="T", plan_id="RP-T", source="S", destination="D", path=RobotPath(path_id="Path-T"),
            targets=[RobotTarget(target_id="source"), RobotTarget(target_id="destination")],
            validation=RobotValidation(publication_verified=True),
            mcp_results=[McpToolResult(tool="manage_path", success=True, data={"command_id":f"command-{i}"}) for i in range(3)])
        self.store.start(plan)
        self.job = self.store.next_job()
        self.receipt = {"global_task_id":"T", "report_key":self.job["report_key"], "status":"accepted", "progress":0,
            "accepted_command_ids":self.job["expected_command_ids"],
            "waypoints_world":[{"x":0,"y":1,"z":0},{"x":1,"y":1,"z":0}]}

    async def test_completion_requires_exact_task_receipts_and_ordered_playback(self):
        with self.assertRaisesRegex(UnityPreviewError, "exact MQTT"):
            self.store.report({**self.receipt, "accepted_command_ids":["previous-command"]})
        with self.assertRaisesRegex(UnityPreviewError, "before playback"):
            self.store.report({**self.receipt, "status":"completed", "progress":1})
        self.store.report(self.receipt)
        with self.assertRaisesRegex(UnityPreviewError, "start before completion"):
            self.store.report({**self.receipt, "status":"completed", "progress":1})
        self.store.report({**self.receipt,"status":"running","progress":.5})
        with self.assertRaisesRegex(UnityPreviewError, "must advance"):
            self.store.report({**self.receipt,"status":"running","progress":.2})
        self.store.report({**self.receipt,"status":"completed","progress":1})
        result = await self.store.wait("T")
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("report_key", self.traces.snapshot()["workflows"][0]["unity"])

    async def test_count_mismatch_and_timeout_never_become_completed(self):
        with self.assertRaisesRegex(UnityPreviewError, "waypoint count"):
            self.store.report({**self.receipt,"waypoints_world":[]})
        with self.assertRaises(UnityPreviewError):
            await self.store.wait("T")
        self.assertEqual(self.traces.snapshot()["workflows"][0]["unity"]["status"], "failed")
        with self.assertRaisesRegex(UnityPreviewError, "already ended"):
            self.store.report({**self.receipt,"status":"completed","progress":1})

    async def test_invalid_coordinates_and_authentication_are_rejected(self):
        with self.assertRaisesRegex(UnityPreviewError, "key"):
            self.store.report({**self.receipt,"report_key":"wrong"})
        with self.assertRaisesRegex(UnityPreviewError, "finite"):
            self.store.report({**self.receipt,"waypoints_world":[{"x":float("nan"),"y":1,"z":0},{"x":1,"y":1,"z":0}]})

    async def test_small_progress_updates_refresh_dashboard_during_slow_playback(self):
        self.store.report(self.receipt)
        for progress in [.01, .02, .03, .05, .07, .1]:
            self.store.report({**self.receipt, "status":"running", "progress":progress})
        snapshot = self.traces.snapshot()
        running = [event for event in snapshot["events"] if event["kind"] == "unity_running"]
        self.assertEqual([event["data"]["progress"] for event in running], [.01, .05, .1])
        self.assertEqual(snapshot["workflows"][0]["unity"]["progress"], .1)


if __name__ == "__main__":
    unittest.main()
