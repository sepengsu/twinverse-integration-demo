"""Supervisor와 A2A 통신 기록을 한정된 메모리에 저장합니다."""

from __future__ import annotations

import copy
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Any
from collections.abc import Callable

from super_agent.supervisor.workflow import SharedTaskState, WorkflowRequest


class WorkflowAlreadyExists(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _redact(value: Any) -> Any:
    # 대시보드와 Kafka 이벤트로 나가기 전 중첩 데이터의 민감 키를 가립니다.
    if isinstance(value, dict):
        sensitive = {"authorization", "password", "api_key", "apikey", "secret", "token", "access_token", "refresh_token"}
        return {
            key: "[redacted]" if str(key).lower().replace("-", "_") in sensitive else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return copy.deepcopy(value)


class TraceStore:
    """최근 작업·이벤트만 보관하고 SSE가 이어받을 번호를 부여합니다."""

    def __init__(self, *, max_workflows: int = 100, max_events: int = 3000) -> None:
        self.max_workflows = max_workflows
        self.events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self.workflows: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.cursor = 0
        self.event_sink: Callable[[dict[str, Any]], None] | None = None

    def begin(self, request: WorkflowRequest) -> None:
        task_id = request.global_task_id
        if task_id in self.workflows:
            raise WorkflowAlreadyExists(f"Workflow already exists: {task_id}")
        if len(self.workflows) >= self.max_workflows:
            # 실행 중인 작업은 삭제하거나 같은 ID로 재사용하지 않습니다.
            finished = next((key for key, row in self.workflows.items() if row["status"] != "working"), None)
            if finished is None:
                raise ValueError("Too many active workflows; wait for an existing workflow to finish.")
            del self.workflows[finished]
        now = _now()
        self.workflows[task_id] = {
            "global_task_id": task_id,
            "created_at": now,
            "updated_at": now,
            "status": "working",
            "request": _redact(request.model_dump(mode="json")),
            "state": None,
            "error": None,
        }
        self.emit(task_id, "workflow_started", source="user", target="supervisor", data=request.model_dump(mode="json"))

    def set_state(self, state: SharedTaskState) -> None:
        row = self.workflows.get(state.global_task_id)
        if row is not None:
            row["state"] = _redact(state.model_dump(mode="json"))
            row["status"] = state.status.value
            row["updated_at"] = _now()

    def finish(self, state: SharedTaskState) -> None:
        self.set_state(state)
        self.emit(state.global_task_id, "workflow_finished", status=state.status.value, data=state.model_dump(mode="json"))

    def fail(self, task_id: str, message: str) -> None:
        row = self.workflows.get(task_id)
        if row is not None:
            row["status"] = "failed"
            row["error"] = message
        self.emit(task_id, "workflow_finished", status="failed", data={"error": message})

    def emit(self, task_id: str, kind: str, **fields: Any) -> dict[str, Any]:
        self.cursor += 1
        event = {
            "id": self.cursor,
            "timestamp": _now(),
            "global_task_id": task_id,
            "kind": kind,
            **_redact(fields),
        }
        self.events.append(event)
        row = self.workflows.get(task_id)
        if row is not None:
            row["updated_at"] = event["timestamp"]
        if self.event_sink is not None:
            self.event_sink(copy.deepcopy(event))
        return event

    def snapshot(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "workflows": copy.deepcopy(list(reversed(self.workflows.values()))),
            "events": copy.deepcopy(list(self.events)),
            "retention": {"workflows": self.max_workflows, "events": self.events.maxlen},
        }

    def events_since(self, cursor: int) -> list[dict[str, Any]]:
        return [copy.deepcopy(event) for event in self.events if event["id"] > cursor]
