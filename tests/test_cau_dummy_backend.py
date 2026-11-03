from __future__ import annotations

import unittest

from agent_shared.config import PROJECT_ROOT
from cau.contracts import ProcessPlanningRequest
from cau.dummy_backend import DummyCauBackend


class DummyCauBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_plan_is_deterministic_and_idempotent(self) -> None:
        backend = DummyCauBackend(PROJECT_ROOT / "config" / "fixtures" / "cau_process_plan.yaml")
        request = ProcessPlanningRequest(
            request_id="REQ-001",
            global_task_id="TASK-001",
            part_id="Part_A",
            requested_machine_id="Machine_01",
        )

        first = await backend.create_process_plan(request)
        second = await backend.create_process_plan(request)

        self.assertEqual(first.plan_id, "PP-001")
        self.assertEqual(first.global_task_id, "TASK-001")
        self.assertEqual(first.operations[0].name, "load")
        self.assertEqual(first.operations[1].name, "machining")
        self.assertEqual(first.operations[2].name, "unload")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
