from __future__ import annotations

import unittest

from pydantic import ValidationError

from super_agent.supervisor.a2a_client import A2AArtifact, A2ATaskResult
from super_agent.supervisor.workflow import WorkflowRequest
from super_agent.supervisor.registry import CapabilityRegistry, AgentEndpoint, AgentRegistration
from super_agent.supervisor.requests import build_direct_request, ProcessPlanToRobotTaskTransformer
from super_agent.supervisor.executor import SupervisorWorkflowExecutor
from super_agent.supervisor.workflow import DeterministicPilotPlanner, InMemoryStateStore


class DirectWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_has_only_selected_skill_and_pipeline_remains_optional(self):
        request = WorkflowRequest(global_task_id="T", goal="Plan", source="source", destination="destination")
        graph = await DeterministicPilotPlanner().create_plan(request)
        self.assertEqual([node.capability for node in graph.tasks.values()], ["robot_path_planning"])
        payload = build_direct_request(request, default_source="fallback")
        self.assertEqual(payload["input"]["source"], "source")
        self.assertEqual(payload["input"]["destination"], "destination")
        self.assertEqual(payload["execution_mode"], "plan_only")
        graph = await DeterministicPilotPlanner().create_plan(request.model_copy(update={"workflow_mode":"process_then_robot","part_id":"P"}))
        self.assertEqual(graph.tasks["T2"].depends_on, ["T1"])

    async def test_registered_new_skill_needs_no_pilot_payload_builder(self):
        calls = []
        class Client:
            async def send_structured_message(self, payload):
                calls.append(payload)
                return A2ATaskResult(task_id="inspection-1", state="completed", artifacts=[
                    A2AArtifact(name="inspection_result", data={"global_task_id":"T","passed":True})])
        endpoint = AgentEndpoint(agent_id="inspection",name="Inspector",base_url="http://localhost:9103",timeout_seconds=3)
        registry = CapabilityRegistry([AgentRegistration(agent_id="inspection",name="Inspector",capabilities=["quality_inspection"],endpoint=endpoint)])
        executor = SupervisorWorkflowExecutor(planner=DeterministicPilotPlanner(),resolver=registry,
            state_store=InMemoryStateStore(),task_transformer=ProcessPlanToRobotTaskTransformer(default_source="source"),a2a_client_factory=lambda endpoint:Client())
        state = await executor.run(WorkflowRequest(global_task_id="T",goal="Inspect",capability="quality_inspection",agent_id="inspection",input={"part_id":"P","global_task_id":"wrong"}))
        self.assertEqual(state.status, "completed")
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]["global_task_id"],"T")
        self.assertEqual(calls[0]["part_id"],"P")
        self.assertEqual(state.artifacts["inspection_result"]["passed"],True)

    async def test_explicit_agent_resolves_duplicates_but_rejects_wrong_skill(self):
        entries = [AgentRegistration(agent_id=name,name=name,capabilities=["inspect"],endpoint=AgentEndpoint(agent_id=name,name=name,base_url="http://localhost:9103",timeout_seconds=3)) for name in ["a","b"]]
        registry = CapabilityRegistry(entries)
        with self.assertRaisesRegex(LookupError,"Multiple enabled"):
            await registry.resolve("inspect")
        self.assertEqual((await registry.resolve("inspect","b")).agent_id,"b")
        with self.assertRaisesRegex(LookupError,"No enabled agent"):
            await registry.resolve("robot_path_planning","b")

    async def test_non_robot_skill_disallows_unity_and_uses_its_own_input(self):
        request = WorkflowRequest(global_task_id="T",goal="Info",capability="process_information",input={"topic":"materials"})
        payload = build_direct_request(request,default_source="source")
        self.assertEqual(payload["topic"],"materials")
        self.assertNotIn("part_id",payload)
        with self.assertRaises(ValidationError):
            WorkflowRequest(global_task_id="T",goal="Info",capability="process_information",metadata={"unity_preview":True})
        with self.assertRaisesRegex(ValueError,"constraints"):
            build_direct_request(request.model_copy(update={"input":{"constraints":"bad"}}),default_source="source")


if __name__ == "__main__":
    unittest.main()
