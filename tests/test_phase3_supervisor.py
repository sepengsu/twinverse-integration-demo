from __future__ import annotations

import asyncio
import socket
import unittest
import uuid
from typing import Any

import httpx
import uvicorn
from fastmcp import FastMCP

from a2a.client import ClientConfig, create_client
from a2a.types.a2a_pb2 import Message, Role, SendMessageConfiguration, SendMessageRequest, Task, TaskState
from a2a.utils.constants import TransportProtocol

from cau.a2a_server import create_cau_app
from agent_shared.a2a import dict_to_data_part, part_to_data
from super_agent.supervisor.workflow import TaskGraph, TaskNode, TaskStatus, WorkflowRequest
from agent_shared.config import (
    A2AEndpointConfig,
    AgentConfig,
    AppConfig,
    BackendConfig,
    DummyBackendConfig,
    McpEndpointConfig,
    PROJECT_ROOT,
    PilotConfig,
    RegistryConfig,
    SupervisorConfig,
)
from cau.contracts import OperationStep, ProcessPlanArtifact
from super_agent.supervisor.registry import CapabilityRegistry, UnsupportedCapabilityError
from skku.app import create_skku_app
from skku.backends.twinverse_mcp import TwinverseMcpBackend
from super_agent.supervisor.app import create_supervisor_app
from super_agent.supervisor.requests import ProcessPlanToRobotTaskTransformer


class Phase3RegistryGraphTransformerTests(unittest.IsolatedAsyncioTestCase):
    async def test_p3_t1_capability_registry_resolves_agents(self) -> None:
        registry = CapabilityRegistry.from_config(_app_config(9100, 9101, 9102))
        resolver = registry

        process_endpoint = await resolver.resolve("process_planning")
        robot_endpoint = await resolver.resolve("robot_path_planning")

        self.assertEqual(process_endpoint.agent_id, "cau_process")
        self.assertEqual(robot_endpoint.agent_id, "skku_robot")

    async def test_p3_t7_unsupported_capability_is_clear(self) -> None:
        registry = CapabilityRegistry.from_config(_app_config(9100, 9101, 9102))

        with self.assertRaisesRegex(UnsupportedCapabilityError, "No enabled agent supports capability"):
            await registry.resolve("unsupported_capability")

    async def test_p3_t2_task_graph_dependency(self) -> None:
        graph = TaskGraph(global_task_id="TASK-GRAPH")
        graph.add_task(TaskNode(task_node_id="T1", capability="process_planning"))
        graph.add_task(TaskNode(task_node_id="T2", capability="robot_path_planning", depends_on=["T1"]))

        self.assertEqual([task.task_node_id for task in graph.ready_tasks()], ["T1"])
        graph.mark_completed("T1")
        self.assertEqual([task.task_node_id for task in graph.ready_tasks()], ["T2"])

    async def test_p3_t3_artifact_transformation(self) -> None:
        process_plan = _process_plan()
        request = WorkflowRequest(
            global_task_id="TASK-001",
            goal="Generate a process plan for Part_A and create the required robot handling plan.",
            part_id="Part_A",
        )
        transformer = ProcessPlanToRobotTaskTransformer(default_source="InputStation")

        robot_request = transformer.transform(process_plan, request)

        self.assertEqual(robot_request.input.destination, "Machine_01")
        self.assertEqual(robot_request.input.part_id, "Part_A")
        self.assertEqual(robot_request.input.source, "InputStation")
        self.assertEqual(robot_request.execution_mode, "plan_only")


class Phase3SupervisorIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.servers: list[uvicorn.Server] = []
        self.server_tasks: list[asyncio.Task[None]] = []
        self.mcp_calls: list[dict[str, Any]] = []

    async def asyncTearDown(self) -> None:
        for server in self.servers:
            server.should_exit = True
        for task in self.server_tasks:
            await asyncio.wait_for(task, timeout=5)

    async def test_default_direct_robot_succeeds_without_cau_server(self):
        cau_port, skku_port, supervisor_port = _free_port(), _free_port(), _free_port()
        await self._start_skku(skku_port)
        await self._start_supervisor(supervisor_port,cau_port,skku_port)
        response = await _post_workflow(f"http://127.0.0.1:{supervisor_port}", {
            "global_task_id":"TASK-DIRECT", "goal":"Plan direct", "part_id":"Part_A",
            "source":"InputStation", "destination":"Machine_01", "agent_id":"skku_robot"})
        self.assertEqual(response["status"],"completed")
        self.assertEqual(list(response["tasks"]),["T1"])
        self.assertEqual(response["tasks"]["T1"]["assigned_agent_id"],"skku_robot")
        self.assertNotIn("process_plan",response["artifacts"])
        self.assertEqual(response["artifacts"]["robot_plan"]["destination"],"Machine_01")

    async def test_direct_cau_information_without_robot_server_or_part(self):
        cau_port, skku_port, supervisor_port = _free_port(), _free_port(), _free_port()
        await self._start_cau(cau_port)
        await self._start_supervisor(supervisor_port,cau_port,skku_port)
        response = await _post_workflow(f"http://127.0.0.1:{supervisor_port}", {
            "global_task_id":"TASK-INFO", "goal":"Info", "capability":"process_information", "agent_id":"cau_process"})
        self.assertEqual(response["status"],"completed")
        self.assertIn("process_information",response["artifacts"])
        self.assertNotIn("robot_plan",response["artifacts"])

    async def test_dashboard_live_launch_records_real_a2a_payloads(self) -> None:
        cau_port, skku_port, supervisor_port = _free_port(), _free_port(), _free_port()
        await self._start_cau(cau_port)
        await self._start_skku(skku_port)
        await self._start_supervisor(supervisor_port, cau_port, skku_port)
        url = f"http://127.0.0.1:{supervisor_port}"
        async with httpx.AsyncClient(timeout=5) as client:
            page = await client.get(f"{url}/dashboard")
            self.assertEqual(page.status_code, 200)
            self.assertIn("A2A Console", page.text)
            script = await client.get(f"{url}/dashboard/assets/app.js")
            self.assertEqual(script.status_code, 200)
            payload = {"global_task_id": "TASK-DASHBOARD", "goal": "Plan a manufacturing workflow.", "part_id": "Part_A", "workflow_mode": "process_then_robot"}
            started = await client.post(f"{url}/dashboard/api/workflows", json=payload)
            self.assertEqual(started.status_code, 202)
            duplicate = await client.post(f"{url}/dashboard/api/workflows", json=payload)
            self.assertEqual(duplicate.status_code, 409)
            for _ in range(60):
                data = (await client.get(f"{url}/dashboard/api/snapshot")).json()
                if data["workflows"] and data["workflows"][0]["status"] != "working":
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(data["workflows"][0]["status"], "completed")
            self.assertIn("robot_plan", data["workflows"][0]["state"]["artifacts"])
            requests = [event for event in data["events"] if event["kind"] == "http_request"]
            self.assertEqual(len(requests), 4)  # Agent Card + message for each real A2A HTTP server.
            messages = [event for event in requests if event["data"]["method"] == "POST"]
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0]["data"]["body"]["message"]["parts"][0]["data"]["capability"], "process_planning")
            self.assertEqual(messages[1]["target"], "skku_robot")
            self.assertTrue(any(event["kind"] == "payload_transformed" for event in data["events"]))
            self.assertEqual(data["events"][-1]["kind"], "workflow_finished")
            # A reconnect can replay events after a known cursor.
            cursor = data["events"][-2]["id"]
            async with client.stream("GET", f"{url}/dashboard/api/events", headers={"Last-Event-ID": str(cursor)}) as response:
                self.assertEqual(response.status_code, 200)
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        import json
                        self.assertEqual(json.loads(line[6:])["kind"], "workflow_finished")
                        break
            agent_rows = (await client.get(f"{url}/dashboard/api/agents")).json()["agents"]
            self.assertTrue(all(agent["status"] == "online" for agent in agent_rows))
            rejected = await client.post(f"{url}/dashboard/api/workflows", json=payload, headers={"Origin": "https://unrelated.example"})
            self.assertEqual(rejected.status_code, 403)

    async def test_dashboard_failure_keeps_cau_result_and_error_trace(self) -> None:
        cau_port, skku_port, supervisor_port = _free_port(), _free_port(), _free_port()
        await self._start_cau(cau_port)
        await self._start_supervisor(supervisor_port, cau_port, skku_port)
        url = f"http://127.0.0.1:{supervisor_port}"
        await _post_workflow(url, {"global_task_id": "TASK-DASHBOARD-FAIL", "goal": "Plan.", "part_id": "Part_A", "workflow_mode": "process_then_robot"})
        async with httpx.AsyncClient(timeout=5) as client:
            data = (await client.get(f"{url}/dashboard/api/snapshot")).json()
            self.assertEqual(data["workflows"][0]["status"], "failed")
            self.assertIn("process_plan", data["workflows"][0]["state"]["artifacts"])
            errors = [event for event in data["events"] if event["kind"] == "delegation_failed"]
            self.assertEqual(errors[0]["capability"], "robot_path_planning")
            agents = (await client.get(f"{url}/dashboard/api/agents")).json()["agents"]
            self.assertEqual(next(agent for agent in agents if agent["id"] == "skku_robot")["status"], "offline")
            invalid = await client.post(f"{url}/dashboard/api/workflows", json={"part_id": "Part_A", "workflow_mode": "process_then_robot"})
            self.assertEqual(invalid.status_code, 422)
            invalid_cursor = await client.get(f"{url}/dashboard/api/events?after=invalid")
            self.assertEqual(invalid_cursor.status_code, 400)

    async def test_p3_t4_full_orchestration_through_supervisor_a2a(self) -> None:
        cau_port, skku_port, supervisor_port = _free_port(), _free_port(), _free_port()
        await self._start_cau(cau_port)
        await self._start_skku(skku_port)
        await self._start_supervisor(supervisor_port, cau_port, skku_port)

        task = await _send_a2a(
            f"http://127.0.0.1:{supervisor_port}",
            {
                "global_task_id": "TASK-P3-001",
                "goal": "Generate a process plan for Part_A and create the required robot handling plan.",
                "part_id": "Part_A",
                "workflow_mode": "process_then_robot",
            },
        )

        self.assertEqual(task.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(task.artifacts[0].name, "shared_task_state")
        state = part_to_data(task.artifacts[0].parts[0])
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["tasks"]["T1"]["assigned_agent_id"], "cau_process")
        self.assertEqual(state["tasks"]["T2"]["assigned_agent_id"], "skku_robot")
        self.assertEqual(state["artifacts"]["process_plan"]["plan_id"], "PP-001")
        self.assertEqual(state["artifacts"]["robot_plan"]["plan_id"], "RP-" + state["global_task_id"])

    async def test_p3_t5_cau_failure_blocks_skku(self) -> None:
        unavailable_cau_port, skku_port, supervisor_port = _free_port(), _free_port(), _free_port()
        await self._start_skku(skku_port)
        await self._start_supervisor(supervisor_port, unavailable_cau_port, skku_port)

        response = await _post_workflow(
            f"http://127.0.0.1:{supervisor_port}",
            {
                "global_task_id": "TASK-P3-CAU-FAIL",
                "goal": "Generate a process plan for Part_A and create the required robot handling plan.",
                "part_id": "Part_A",
                "workflow_mode": "process_then_robot",
            },
        )

        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["tasks"]["T1"]["status"], "failed")
        self.assertEqual(response["tasks"]["T2"]["status"], "blocked")
        self.assertEqual(self.mcp_calls, [])

    async def test_p3_t6_skku_failure_retains_process_plan(self) -> None:
        cau_port, unavailable_skku_port, supervisor_port = _free_port(), _free_port(), _free_port()
        await self._start_cau(cau_port)
        await self._start_supervisor(supervisor_port, cau_port, unavailable_skku_port)

        response = await _post_workflow(
            f"http://127.0.0.1:{supervisor_port}",
            {
                "global_task_id": "TASK-P3-SKKU-FAIL",
                "goal": "Generate a process plan for Part_A and create the required robot handling plan.",
                "part_id": "Part_A",
                "workflow_mode": "process_then_robot",
            },
        )

        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["tasks"]["T1"]["status"], "completed")
        self.assertEqual(response["tasks"]["T2"]["status"], "failed")
        self.assertIn("process_plan", response["artifacts"])
        self.assertEqual(response["artifacts"]["process_plan"]["plan_id"], "PP-001")

    async def test_p3_t8_physical_execution_safety(self) -> None:
        cau_port, skku_port, supervisor_port = _free_port(), _free_port(), _free_port()
        await self._start_cau(cau_port)
        await self._start_skku(skku_port)
        await self._start_supervisor(supervisor_port, cau_port, skku_port)

        response = await _post_workflow(
            f"http://127.0.0.1:{supervisor_port}",
            {
                "global_task_id": "TASK-P3-SAFE",
                "goal": "Generate a process plan for Part_A and create the required robot handling plan.",
                "part_id": "Part_A",
                "workflow_mode": "process_then_robot",
            },
        )

        self.assertEqual(response["status"], "completed")
        actions = [call["arguments"]["action"] for call in self.mcp_calls if call["tool"] == "manage_path"]
        self.assertEqual(actions, ["make_path"] + ["make_object_anchor_target"] * 4 + ["connect_targets"])
        self.assertNotIn("start_path", actions)
        self.assertNotIn("real_start", actions)
        self.assertNotIn("simulation_start", actions)
        self.assertEqual(response["artifacts"]["robot_plan"]["execution_mode"], "plan_only")

    async def _start_cau(self, port: int) -> None:
        base_url = f"http://127.0.0.1:{port}"
        agent = AgentConfig(
            name="cau-process-agent-test",
            enabled=True,
            mode="dummy",
            description="Test CAU A2A agent",
            capabilities=["process_planning", "equipment_selection", "process_information"],
            a2a=A2AEndpointConfig(host="127.0.0.1", port=port, base_url=base_url, streaming=False),
            backend=BackendConfig(type="dummy"),
            dummy=DummyBackendConfig(fixture_file=PROJECT_ROOT / "config" / "fixtures" / "cau_process_plan.yaml"),
        )
        await self._start_app(create_cau_app(agent), port)

    async def _start_skku(self, port: int) -> None:
        base_url = f"http://127.0.0.1:{port}"
        fake_mcp, self.mcp_calls = _fake_twinverse_mcp()
        agent = AgentConfig(
            name="skku-twinverse-agent-test",
            enabled=True,
            mode="remote",
            description="Test SKKU Twinverse robot A2A agent",
            capabilities=["robot_path_planning", "robot_state_query", "path_validation"],
            a2a=A2AEndpointConfig(host="127.0.0.1", port=port, base_url=base_url, streaming=False),
            backend=BackendConfig(type="mcp"),
            mcp=McpEndpointConfig(transport="streamable-http", url="http://127.0.0.1:8000/mcp", timeout_seconds=2),
        )
        await self._start_app(create_skku_app(agent, backend=TwinverseMcpBackend(fake_mcp, timeout_seconds=2)), port)

    async def _start_supervisor(self, supervisor_port: int, cau_port: int, skku_port: int) -> None:
        config = _app_config(supervisor_port, skku_port, cau_port)
        await self._start_app(create_supervisor_app(config), supervisor_port)

    async def _start_app(self, app: Any, port: int) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        self.servers.append(server)
        self.server_tasks.append(task)
        await _wait_for_http_ok(f"http://127.0.0.1:{port}/health")


def _app_config(supervisor_port: int, skku_port: int, cau_port: int) -> AppConfig:
    return AppConfig(
        environment="test",
        supervisor=SupervisorConfig(
            name="super-agent-test",
            host="127.0.0.1",
            port=supervisor_port,
            log_level="WARNING",
            request_timeout_seconds=5,
        ),
        registry=RegistryConfig(allow_first_candidate_for_duplicates=False),
        pilot=PilotConfig(default_source="InputStation"),
        agents={
            "skku_robot": AgentConfig(
                name="skku-twinverse-agent-test",
                enabled=True,
                mode="remote",
                description="Test SKKU Twinverse robot A2A agent",
                capabilities=["robot_path_planning", "robot_state_query", "path_validation"],
                a2a=A2AEndpointConfig(
                    host="127.0.0.1",
                    port=skku_port,
                    base_url=f"http://127.0.0.1:{skku_port}",
                    timeout_seconds=2,
                    streaming=False,
                ),
                backend=BackendConfig(type="mcp"),
                mcp=McpEndpointConfig(transport="streamable-http", url="http://127.0.0.1:8000/mcp", timeout_seconds=2),
            ),
            "cau_process": AgentConfig(
                name="cau-process-agent-test",
                enabled=True,
                mode="dummy",
                description="Test CAU A2A agent",
                capabilities=["process_planning", "equipment_selection", "process_information"],
                a2a=A2AEndpointConfig(
                    host="127.0.0.1",
                    port=cau_port,
                    base_url=f"http://127.0.0.1:{cau_port}",
                    timeout_seconds=2,
                    streaming=False,
                ),
                backend=BackendConfig(type="dummy"),
                dummy=DummyBackendConfig(fixture_file=PROJECT_ROOT / "config" / "fixtures" / "cau_process_plan.yaml"),
            ),
        },
    )


def _process_plan() -> ProcessPlanArtifact:
    return ProcessPlanArtifact(
        request_id="REQ-PP-TASK-001",
        global_task_id="TASK-001",
        plan_id="PP-001",
        part_id="Part_A",
        machine_id="Machine_01",
        operations=[
            OperationStep(
                operation_id="OP-001",
                name="load",
                sequence=1,
                part_id="Part_A",
                resource_id="Machine_01",
                estimated_duration_seconds=5,
            )
        ],
    )


def _fake_twinverse_mcp() -> tuple[FastMCP, list[dict[str, Any]]]:
    mcp = FastMCP("fake-twinverse")
    calls: list[dict[str, Any]] = []

    @mcp.tool(name="manage_path")
    async def manage_path(action: str, json: dict[str, Any]) -> dict[str, Any]:
        calls.append({"tool": "manage_path", "arguments": {"action": action, "json": json}})
        blocked = {"start_path", "real_start", "simulation_start", "move_tcp", "homing"}
        if action in blocked:
            return {"success": False, "message": f"blocked in fake test: {action}"}
        return {
            "success": True,
            "message": f"Published {action} over MQTT.",
            "data": {
                "transport": "mqtt",
                "command_action": action,
                "command_id": f"command-{len(calls)}",
                "object_id": json.get("reference_object_id") or "skku/gp25_target",
                "path": json.get("path") or json.get("name"),
            },
        }

    @mcp.tool(name="see_virtual_objects")
    async def see_virtual_objects() -> list[str]:
        calls.append({"tool": "see_virtual_objects", "arguments": {}})
        return ["InputStation", "Machine_01", "skku/work_table_01"]

    @mcp.tool(name="read_console")
    async def read_console(
        action: str | None = None,
        types: list[str] | None = None,
        count: int | None = None,
        format: str | None = None,
        include_stacktrace: bool | None = None,
    ) -> dict[str, Any]:
        calls.append(
            {
                "tool": "read_console",
                "arguments": {
                    "action": action,
                    "types": types,
                    "count": count,
                    "format": format,
                    "include_stacktrace": include_stacktrace,
                },
            }
        )
        return {"success": True, "data": []}

    return mcp, calls


async def _send_a2a(base_url: str, payload: dict[str, Any]) -> Task:
    httpx_client = httpx.AsyncClient(timeout=5)
    client = await create_client(
        base_url,
        client_config=ClientConfig(
            streaming=False,
            httpx_client=httpx_client,
            supported_protocol_bindings=[TransportProtocol.HTTP_JSON.value],
        ),
    )
    request = SendMessageRequest(
        message=Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[dict_to_data_part(payload)],
        ),
        configuration=SendMessageConfiguration(history_length=10),
    )
    try:
        responses = [response async for response in client.send_message(request)]
    finally:
        await client.close()
    return responses[0].task


async def _post_workflow(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.post(f"{base_url}/workflow", json=payload)
        response.raise_for_status()
        data = response.json()
        assert isinstance(data, dict)
        return data


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_http_ok(url: str) -> None:
    async with httpx.AsyncClient(timeout=0.5) as client:
        for _ in range(50):
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                await asyncio.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for {url}")


if __name__ == "__main__":
    unittest.main()
