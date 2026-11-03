"""Supervisor가 기관 Agent를 호출할 때 사용하는 공식 A2A SDK 래퍼입니다."""

from __future__ import annotations

import uuid
import json
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from a2a.client import ClientConfig, create_client
from a2a.types.a2a_pb2 import Message, Role, SendMessageConfiguration, SendMessageRequest, TaskState
from a2a.utils.constants import TransportProtocol

from agent_shared.a2a import dict_to_data_part, part_to_data
from super_agent.supervisor.registry import AgentEndpoint


class A2AArtifact(BaseModel):
    """Structured artifact extracted from an A2A task response."""

    model_config = ConfigDict(extra="forbid")

    name: str
    data: dict[str, Any]
    artifact_id: str | None = None


class A2ATaskResult(BaseModel):
    """SDK-independent result consumed by the supervisor execution loop."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    state: str
    artifacts: list[A2AArtifact] = Field(default_factory=list)

    def artifact_data(self, name: str) -> dict[str, Any] | None:
        for artifact in self.artifacts:
            if artifact.name == name:
                return artifact.data
        return None

    def first_artifact_data(self) -> dict[str, Any] | None:
        return self.artifacts[0].data if self.artifacts else None


class A2AAgentClient:
    """구조화된 요청을 보내고 Task 상태·Artifact를 Supervisor 형식으로 돌려줍니다."""

    def __init__(self, endpoint: AgentEndpoint) -> None:
        self.endpoint = endpoint

    async def send_structured_message(
        self,
        payload: dict[str, Any],
        *,
        observer: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> A2ATaskResult:
        async def observe_request(request: httpx.Request) -> None:
            if observer is not None:
                observer("http_request", {
                    "method": request.method,
                    "url": str(request.url.copy_with(username=None, password=None)),
                    "body": _json_body(request.content),
                })

        async def observe_response(response: httpx.Response) -> None:
            if observer is not None:
                # SSE 응답 본문은 SDK가 읽어야 하므로 관찰용 기록에서 소비하지 않습니다.
                is_stream = "text/event-stream" in response.headers.get("content-type", "")
                body = None if is_stream else _json_body(await response.aread())
                observer("http_response", {
                    "method": response.request.method,
                    "url": str(response.request.url.copy_with(username=None, password=None)),
                    "status_code": response.status_code,
                    "body": body,
                })

        httpx_client = httpx.AsyncClient(
            timeout=self.endpoint.timeout_seconds,
            event_hooks={"request": [observe_request], "response": [observe_response]} if observer else None,
        )
        client = None
        try:
            client = await create_client(
                self.endpoint.base_url,
                client_config=ClientConfig(
                    streaming=self.endpoint.streaming,
                    httpx_client=httpx_client,
                    supported_protocol_bindings=[TransportProtocol.HTTP_JSON.value],
                ),
            )
            request = SendMessageRequest(
                # 기관 간 입력은 텍스트 설명이 아닌 구조화된 A2A DataPart로 전달합니다.
                message=Message(
                    message_id=str(uuid.uuid4()),
                    role=Role.ROLE_USER,
                    parts=[dict_to_data_part(payload)],
                ),
                configuration=SendMessageConfiguration(history_length=10),
            )
            responses = [response async for response in client.send_message(request)]
        except Exception as exc:
            raise A2AClientError(f"A2A call to {self.endpoint.agent_id} failed: {exc}") from exc
        finally:
            if client is not None:
                await client.close()
            else:
                await httpx_client.aclose()

        if not responses:
            raise A2AClientError(f"A2A call to {self.endpoint.agent_id} returned no responses.")

        task = responses[-1].task
        # A2A Task에서 각 Artifact의 DataPart를 꺼내 실행 루프가 검증할 수 있게 합니다.
        artifacts: list[A2AArtifact] = []
        for artifact in task.artifacts:
            for part in artifact.parts:
                data = part_to_data(part)
                if data is not None:
                    artifacts.append(
                        A2AArtifact(
                            name=artifact.name,
                            artifact_id=getattr(artifact, "artifact_id", None) or None,
                            data=data,
                        )
                    )
                    break

        return A2ATaskResult(
            task_id=task.id,
            state=_task_state_name(task.status.state),
            artifacts=artifacts,
        )


class A2AClientError(RuntimeError):
    """Raised when an A2A delegation cannot be completed."""


def _task_state_name(state: int) -> str:
    try:
        return TaskState.Name(state).removeprefix("TASK_STATE_").lower()
    except ValueError:
        return str(state)


def _json_body(body: bytes) -> Any:
    if not body:
        return None
    if len(body) > 256_000:
        return {"truncated": True, "byte_length": len(body)}
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body[:2000].decode("utf-8", errors="replace")
