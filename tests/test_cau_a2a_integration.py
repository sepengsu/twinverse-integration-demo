from __future__ import annotations

import asyncio
import socket
import unittest
import uuid

import httpx
import uvicorn

from a2a.client import ClientConfig, create_client
from a2a.types.a2a_pb2 import Message, Role, SendMessageConfiguration, SendMessageRequest, TaskState
from a2a.utils.constants import TransportProtocol

from agent_shared.a2a import dict_to_data_part, part_to_data
from agent_shared.config import A2AEndpointConfig, AgentConfig, BackendConfig, DummyBackendConfig, PROJECT_ROOT
from cau.a2a_server import create_cau_app


class CauA2AIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        agent = AgentConfig(
            name="cau-process-agent-test",
            enabled=True,
            mode="dummy",
            description="Test CAU A2A agent",
            capabilities=["process_planning", "equipment_selection", "process_information"],
            a2a=A2AEndpointConfig(host="127.0.0.1", port=self.port, base_url=self.base_url, streaming=False),
            backend=BackendConfig(type="dummy"),
            dummy=DummyBackendConfig(fixture_file=PROJECT_ROOT / "config" / "fixtures" / "cau_process_plan.yaml"),
        )
        app = create_cau_app(agent)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.server_task = asyncio.create_task(self.server.serve())
        await _wait_for_http_ok(f"{self.base_url}/health")

    async def asyncTearDown(self) -> None:
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, timeout=5)

    async def test_official_client_receives_process_plan_artifact(self) -> None:
        httpx_client = httpx.AsyncClient(timeout=5)
        client = await create_client(
            self.base_url,
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
                parts=[
                    dict_to_data_part(
                        {
                            "schema_version": "1.0",
                            "request_id": "REQ-001",
                            "global_task_id": "TASK-001",
                            "capability": "process_planning",
                            "part_id": "Part_A",
                            "requested_machine_id": "Machine_01",
                        }
                    )
                ],
            ),
            configuration=SendMessageConfiguration(history_length=10),
        )

        responses = [response async for response in client.send_message(request)]
        await client.close()

        self.assertEqual(len(responses), 1)
        task = responses[0].task
        self.assertEqual(task.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(task.artifacts[0].name, "process_plan")

        payload = part_to_data(task.artifacts[0].parts[0])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["global_task_id"], "TASK-001")
        self.assertEqual(payload["plan_id"], "PP-001")
        self.assertEqual([operation["name"] for operation in payload["operations"]], ["load", "machining", "unload"])


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
