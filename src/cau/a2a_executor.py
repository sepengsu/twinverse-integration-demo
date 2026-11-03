"""A2A 요청을 CAU 공정 백엔드 호출과 결과 Artifact로 연결합니다."""

from __future__ import annotations

import json
import time
from typing import Any, cast

from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ValidationError

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Message, Part, Role, Task, TaskState, TaskStatus

from cau.backend import CauBackend
from cau.contracts import (
    EquipmentSelectionRequest,
    ErrorArtifact,
    ProcessInformationRequest,
    ProcessPlanningRequest,
)
from agent_shared.a2a import dict_to_data_part, part_to_data, timestamp_now
from agent_shared.demo_pacing import finish_demo_stage


class CauA2AExecutor(AgentExecutor):
    """공식 A2A 작업 상태를 CAU의 세 기능 및 오류 상태에 대응시킵니다."""

    def __init__(self, backend: CauBackend) -> None:
        self.backend = backend

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # 먼저 제출 상태를 알리고, 작업 시작→Artifact 발행→완료 순으로 진행합니다.
        task_id = cast(str, context.task_id)
        context_id = cast(str, context.context_id)
        await event_queue.enqueue_event(self._initial_task(context, task_id, context_id))

        updater = TaskUpdater(event_queue, task_id, context_id)

        try:
            payload = self._extract_payload(context.message)
            capability = self._extract_capability(payload)
            await updater.start_work(self._agent_text(updater, f"CAU dummy backend started: {capability}"))
            stage_started = time.monotonic()
            result = await self._dispatch(capability, payload)
            if capability == "process_planning":
                await finish_demo_stage(stage_started)
            await updater.add_artifact(
                [dict_to_data_part(result.model_dump(mode="json"))],
                artifact_id=self._artifact_id(result),
                name=result.artifact_type,
                metadata={
                    "global_task_id": result.global_task_id,
                    "request_id": result.request_id,
                    "capability": capability,
                },
                last_chunk=True,
            )
            await updater.complete(self._agent_text(updater, f"CAU dummy backend completed: {capability}"))
        except UnsupportedCapability as exc:
            # 미지원 기능은 거절, 보완 가능한 입력 오류는 input_required로 구분합니다.
            await self._publish_error(updater, "unsupported_capability", str(exc), payload=exc.payload)
            await updater.reject(self._agent_text(updater, str(exc)))
        except InputRequired as exc:
            await self._publish_error(updater, "input_required", str(exc), details=exc.details, payload=exc.payload)
            await updater.requires_input(self._agent_text(updater, str(exc)))
        except ValidationError as exc:
            details = [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]
            await self._publish_error(updater, "invalid_input", "CAU request payload is invalid.", details=details)
            await updater.requires_input(self._agent_text(updater, "CAU request payload is invalid."))
        except Exception as exc:
            await self._publish_error(updater, "backend_failed", str(exc))
            await updater.failed(self._agent_text(updater, f"CAU backend failed: {exc}"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = cast(str, context.task_id)
        context_id = cast(str, context.context_id)
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.cancel(self._agent_text(updater, "CAU task was canceled."))

    async def _dispatch(self, capability: str, payload: dict[str, Any]) -> BaseModel:
        match capability:
            case "process_planning":
                return await self.backend.create_process_plan(ProcessPlanningRequest.model_validate(payload))
            case "equipment_selection":
                return await self.backend.select_equipment(EquipmentSelectionRequest.model_validate(payload))
            case "process_information":
                return await self.backend.get_process_information(ProcessInformationRequest.model_validate(payload))
            case _:
                raise UnsupportedCapability(f"Unsupported CAU capability: {capability}", payload=payload)

    def _extract_capability(self, payload: dict[str, Any]) -> str:
        capability = payload.get("capability")
        if not isinstance(capability, str) or not capability:
            raise InputRequired("CAU request payload must include a string capability.", payload=payload)
        return capability

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
        # 구조화된 DataPart를 우선 읽고, JSON 텍스트 요청도 허용합니다.
        if message is None:
            raise InputRequired("A2A message is required.")

        text_parts: list[str] = []
        for part in message.parts:
            data = part_to_data(part)
            if data is not None:
                return data
            if part.WhichOneof("content") == "text":
                text_parts.append(part.text)

        if not text_parts:
            raise InputRequired("A2A message must contain a data part or JSON text.")

        try:
            payload = json.loads("\n".join(text_parts))
        except json.JSONDecodeError as exc:
            raise InputRequired("Text message must be valid JSON.", details=[str(exc)]) from exc

        if not isinstance(payload, dict):
            raise InputRequired("JSON text payload must be an object.")
        return payload

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
        return updater.new_agent_message([Part(text=text)], metadata={"source": "cau_process"})

    @staticmethod
    def _artifact_id(result: BaseModel) -> str:
        data = MessageToDict(dict_to_data_part(result.model_dump(mode="json")).data)
        return str(data.get("plan_id") or data.get("selection_id") or data.get("information_id") or "artifact")


class InputRequired(ValueError):
    def __init__(self, message: str, *, details: list[str] | None = None, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or []
        self.payload = payload or {}


class UnsupportedCapability(ValueError):
    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = payload or {}
