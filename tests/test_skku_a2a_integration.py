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

from agent_shared.a2a import dict_to_data_part, part_to_data
from agent_shared.config import A2AEndpointConfig, AgentConfig, BackendConfig, McpEndpointConfig
from skku.app import create_skku_app
from skku.backends.twinverse_mcp import TwinverseMcpBackend


class SkkuA2AIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.fake_mcp, self.mcp_calls = _fake_twinverse_mcp()
        self.agent = _agent_config(self.port, self.base_url)
        self.backend = TwinverseMcpBackend(self.fake_mcp, timeout_seconds=2)
        app = create_skku_app(self.agent, backend=self.backend)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve())
        await _wait_for_http_ok(f"{self.base_url}/health")

    async def asyncTearDown(self) -> None:
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, timeout=5)

    async def test_p2_t1_robot_state_query(self) -> None:
        task = await self._send(
            {
                "schema_version": "1.0",
                "request_id": "REQ-S-001",
                "global_task_id": "TASK-S-001",
                "capability": "robot_state_query",
                "input": {},
                "execution_mode": "plan_only",
            }
        )

        self.assertEqual(task.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(task.artifacts[0].name, "robot_state")
        payload = part_to_data(task.artifacts[0].parts[0])
        self.assertIsNotNone(payload)
        self.assertIn("InputStation", payload["objects"])
        self.assertIn("Machine_01", payload["objects"])

    async def test_p2_t2_robot_path_planning_calls_fastmcp_and_returns_artifact(self) -> None:
        task = await self._send(
            {
                "schema_version": "1.0",
                "request_id": "REQ-RP-001",
                "global_task_id": "TASK-001",
                "capability": "robot_path_planning",
                "input": {
                    "goal": "Move Part_A from InputStation to Machine_01",
                    "part_id": "Part_A",
                    "source": "InputStation",
                    "destination": "Machine_01",
                    "target_equipment_id": "Machine_01",
                    "constraints": {
                        "collision_free": True,
                        "known_objects": ["InputStation", "Machine_01"],
                    },
                },
                "execution_mode": "plan_only",
            }
        )

        self.assertEqual(task.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(task.artifacts[0].name, "robot_plan")
        payload = part_to_data(task.artifacts[0].parts[0])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["global_task_id"], "TASK-001")
        self.assertEqual(payload["plan_id"], "RP-TASK-001")
        self.assertEqual(payload["status"], "planned")
        self.assertTrue(payload["path"]["path_id"].startswith("Path-"))
        self.assertIsNone(payload["validation"]["collision_free"])
        self.assertEqual(payload["validation"]["collision_check"], "not_performed")
        self.assertTrue(payload["validation"]["publication_verified"])
        self.assertEqual([target["role"] for target in payload["targets"]], ["source_anchor", "lift", "approach", "destination_anchor"])
        self.assertEqual(payload["targets"][0]["object_id"], "InputStation")
        self.assertEqual(payload["targets"][-1]["object_id"], "Machine_01")

        actions = [call["arguments"]["action"] for call in self.mcp_calls if call["tool"] == "manage_path"]
        self.assertEqual(actions, ["make_path"] + ["make_object_anchor_target"] * 4 + ["connect_targets"])
        self.assertNotIn("start_path", actions)
        self.assertNotIn("real_start", actions)

    async def test_p2_t3_invalid_object_returns_input_required(self) -> None:
        task = await self._send(
            {
                "schema_version": "1.0",
                "request_id": "REQ-RP-002",
                "global_task_id": "TASK-002",
                "capability": "robot_path_planning",
                "input": {
                    "source": "InputStation",
                    "destination": "MissingMachine",
                    "constraints": {
                        "known_objects": ["InputStation", "Machine_01"],
                    },
                },
                "execution_mode": "plan_only",
            }
        )

        self.assertEqual(task.status.state, TaskState.TASK_STATE_INPUT_REQUIRED)
        self.assertEqual(task.artifacts[0].name, "error")
        payload = part_to_data(task.artifacts[0].parts[0])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["error_code"], "unknown_object")
        self.assertIn("MissingMachine", payload["message"])

    async def test_p2_t4_fastmcp_unavailable_maps_to_failed(self) -> None:
        unavailable_port = _free_port()
        failing_backend = TwinverseMcpBackend(f"http://127.0.0.1:{unavailable_port}/mcp", timeout_seconds=0.2)
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        app = create_skku_app(_agent_config(port, base_url), backend=failing_backend)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        server_task = asyncio.create_task(server.serve())
        await _wait_for_http_ok(f"{base_url}/health")

        try:
            task = await _send_to(
                base_url,
                {
                    "schema_version": "1.0",
                    "request_id": "REQ-S-004",
                    "global_task_id": "TASK-S-004",
                    "capability": "robot_state_query",
                    "input": {},
                    "execution_mode": "plan_only",
                },
            )
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5)

        self.assertEqual(task.status.state, TaskState.TASK_STATE_FAILED)
        payload = part_to_data(task.artifacts[0].parts[0])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["error_code"], "mcp_unavailable")

    async def _send(self, payload: dict[str, Any]) -> Task:
        return await _send_to(self.base_url, payload)


def _fake_twinverse_mcp() -> tuple[FastMCP, list[dict[str, Any]]]:
    mcp = FastMCP("fake-twinverse")
    calls: list[dict[str, Any]] = []

    @mcp.tool(name="manage_path")
    async def manage_path(action: str, json: dict[str, Any]) -> dict[str, Any]:
        calls.append({"tool": "manage_path", "arguments": {"action": action, "json": json}})
        blocked = {"start_path", "move_tcp", "move_tcp_to_object_anchor", "homing"}
        if action in blocked:
            return {"success": False, "message": f"blocked in fake test: {action}"}
        return {
            "success": True,
            "message": f"Published {action} over MQTT.",
            "data": {
                "transport": "mqtt",
                "legacy_action": action,
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


def _agent_config(port: int, base_url: str) -> AgentConfig:
    return AgentConfig(
        name="skku-twinverse-agent-test",
        enabled=True,
        mode="remote",
        description="Test SKKU Twinverse robot A2A agent",
        capabilities=["robot_path_planning", "robot_state_query", "path_validation"],
        a2a=A2AEndpointConfig(host="127.0.0.1", port=port, base_url=base_url, streaming=False),
        backend=BackendConfig(type="mcp"),
        mcp=McpEndpointConfig(transport="streamable-http", url="http://127.0.0.1:8000/mcp", timeout_seconds=2),
    )


async def _send_to(base_url: str, payload: dict[str, Any]) -> Task:
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
