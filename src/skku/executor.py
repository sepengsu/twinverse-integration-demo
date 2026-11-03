"""SKKU의 A2A 요청을 Twinverse 경로 계획·조회·검증 기능으로 연결합니다."""

from __future__ import annotations

import json
import time
from typing import Any, cast

from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ValidationError

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Message, Part, Task, TaskState, TaskStatus

from agent_shared.a2a import dict_to_data_part, part_to_data, timestamp_now
from agent_shared.demo_pacing import finish_demo_stage
from skku.backend import (
    McpToolUnavailableError,
    McpUnavailableError,
    PathGenerationError,
    PathValidationError,
    RobotBackend,
    RobotBackendError,
    SafetyViolationError,
    UnknownRobotObjectError,
)
from skku.models import ErrorArtifact, RobotTaskRequest


class SkkuA2AExecutor(AgentExecutor):
    """A2A 작업 상태와 SKKU 로봇 백엔드의 결과·오류를 대응시킵니다."""

    def __init__(self, backend: RobotBackend) -> None:
        self.backend = backend

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 입력 검증이 통과한 뒤에만 MCP 백엔드를 호출합니다.
        task_id = cast(str, context.task_id)
        context_id = cast(str, context.context_id)
        await event_queue.enqueue_event(self._initial_task(context, task_id, context_id))
        updater = TaskUpdater(event_queue, task_id, context_id)

        payload: dict[str, Any] = {}
        try:
            payload = self._extract_payload(context.message)
            capability = self._extract_capability(payload)
            request = RobotTaskRequest.model_validate(payload)
            if request.capability != capability:
                raise UnsupportedCapability(f"Capability mismatch: {capability}", payload=payload)

            await updater.start_work(self._agent_text(updater, f"SKKU Twinverse backend started: {capability}"))
            stage_started = time.monotonic()
            result = await self._dispatch(request)
            if capability == "robot_path_planning":
                await finish_demo_stage(stage_started)
            await updater.add_artifact(
                [dict_to_data_part(result.model_dump(mode="json"))],
                artifact_id=self._artifact_id(result),
                name=result.artifact_type,
                metadata={
                    "global_task_id": request.global_task_id,
                    "request_id": request.request_id or "",
                    "capability": capability,
                    "execution_mode": request.execution_mode,
                },
                last_chunk=True,
            )
            await updater.complete(self._agent_text(updater, f"SKKU Twinverse backend completed: {capability}"))
        except UnsupportedCapability as exc:
            await self._publish_error(updater, "unsupported_capability", str(exc), payload=exc.payload)
            await updater.reject(self._agent_text(updater, str(exc)))
        except ValidationError as exc:
            details = [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]
            await self._publish_error(updater, "invalid_request_schema", "SKKU robot request payload is invalid.", details=details, payload=payload)
            await updater.requires_input(self._agent_text(updater, "SKKU robot request payload is invalid."))
        except UnknownRobotObjectError as exc:
            # 참조 개체를 찾지 못하면 사용자 입력을 보완할 수 있도록 요청 상태를 남깁니다.
            await self._publish_error(updater, "unknown_object", str(exc), payload=payload)
            await updater.requires_input(self._agent_text(updater, str(exc)))
        except SafetyViolationError as exc:
            # 계획 전용 모드에서 실행 명령이 요청되면 A2A 작업을 거절합니다.
            await self._publish_error(updater, "blocked_execution_action", str(exc), payload=payload)
            await updater.reject(self._agent_text(updater, str(exc)))
        except (McpUnavailableError, McpToolUnavailableError) as exc:
            await self._publish_error(updater, "mcp_unavailable", str(exc), payload=payload)
            await updater.failed(self._agent_text(updater, str(exc)))
        except (PathGenerationError, PathValidationError) as exc:
            await self._publish_error(updater, "robot_plan_failed", str(exc), payload=payload)
            await updater.failed(self._agent_text(updater, str(exc)))
        except RobotBackendError as exc:
            await self._publish_error(updater, "robot_backend_failed", str(exc), payload=payload)
            await updater.failed(self._agent_text(updater, str(exc)))
        except Exception as exc:
            await self._publish_error(updater, "unexpected_failure", str(exc), payload=payload)
            await updater.failed(self._agent_text(updater, f"SKKU robot backend failed: {exc}"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = cast(str, context.task_id)
        context_id = cast(str, context.context_id)
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.cancel(self._agent_text(updater, "SKKU robot task was canceled."))

    async def _dispatch(self, request: RobotTaskRequest) -> BaseModel:
        match request.capability:
            case "robot_path_planning":
                return await self.backend.plan_robot_task(request)
            case "robot_state_query":
                return await self.backend.get_robot_state(request)
            case "path_validation":
                return await self.backend.validate_robot_plan(request)
            case _:
                raise UnsupportedCapability(f"Unsupported SKKU robot capability: {request.capability}")

    def _initial_task(self, context: RequestContext, task_id: str, context_id: str) -> Task:
        history = [context.message] if context.message is not None else []
        return Task(
            id=task_id,
            context_id=context_id,
            status=TaskStatus(
                state=TaskState.TASK_STATE_SUBMITTED,
                timestamp=timestamp_now(),
            ),
            history=history,
        )

    def _extract_payload(self, message: Message | None) -> dict[str, Any]:
        if message is None:
            raise UnknownRobotObjectError("A2A message is required.")

        text_parts: list[str] = []
        for part in message.parts:
            data = part_to_data(part)
            if data is not None:
                return data
            if part.WhichOneof("content") == "text":
                text_parts.append(part.text)

        if not text_parts:
            raise UnknownRobotObjectError("A2A message must contain a data part or JSON text.")

        try:
            payload = json.loads("\n".join(text_parts))
        except json.JSONDecodeError as exc:
            raise UnknownRobotObjectError(f"Text message must be valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise UnknownRobotObjectError("JSON text payload must be an object.")
        return payload

    def _extract_capability(self, payload: dict[str, Any]) -> str:
        capability = payload.get("capability")
        if not isinstance(capability, str) or not capability:
            raise UnknownRobotObjectError("SKKU robot request payload must include a string capability.")
        allowed = {"robot_path_planning", "robot_state_query", "path_validation"}
        if capability not in allowed:
            raise UnsupportedCapability(f"Unsupported SKKU robot capability: {capability}", payload=payload)
        return capability

    async def _publish_error(
        self,
        updater: TaskUpdater,
        error_code: str,
        message: str,
        *,
        details: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        payload = payload or {}
        error = ErrorArtifact(
            request_id=payload.get("request_id"),
            global_task_id=payload.get("global_task_id"),
            capability=payload.get("capability"),
            error_code=error_code,
            message=message,
            details=details or [],
        )
        await updater.add_artifact(
            [dict_to_data_part(error.model_dump(mode="json"))],
            artifact_id=f"error-{error_code}",
            name="error",
            last_chunk=True,
        )

    @staticmethod
    def _agent_text(updater: TaskUpdater, text: str) -> Message:
        return updater.new_agent_message([Part(text=text)], metadata={"source": "skku_robot"})

    @staticmethod
    def _artifact_id(result: BaseModel) -> str:
        data = MessageToDict(dict_to_data_part(result.model_dump(mode="json")).data)
        return str(data.get("plan_id") or data.get("state_id") or data.get("validation_id") or "artifact")


class UnsupportedCapability(ValueError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}
