"""사용자 요청을 기관별 작업으로 나누고, 진행 상태를 메모리에 보관합니다."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskStatus(StrEnum):
    """Lifecycle values for a supervisor task node."""

    PENDING = "pending"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    INPUT_REQUIRED = "input_required"


class TaskNode(BaseModel):
    """A single capability-level unit of work in the supervisor DAG."""

    model_config = ConfigDict(extra="forbid")

    task_node_id: str
    capability: str
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = Field(default_factory=list)
    assigned_agent_id: str | None = None
    a2a_task_id: str | None = None
    error: str | None = None


class TaskGraph(BaseModel):
    """이 시연에서 필요한 1~2단계 의존 관계만 표현하는 작업 그래프."""

    model_config = ConfigDict(extra="forbid")

    global_task_id: str
    tasks: dict[str, TaskNode] = Field(default_factory=dict)

    def add_task(self, node: TaskNode) -> None:
        if node.task_node_id in self.tasks:
            raise TaskGraphError(f"Duplicate task node id: {node.task_node_id}")
        self.tasks[node.task_node_id] = node

    def dependencies_for(self, task_node_id: str) -> list[str]:
        return list(self.require_task(task_node_id).depends_on)

    def ready_tasks(self) -> list[TaskNode]:
        # 앞 단계가 완료된 작업만 반환해 CAU 결과 없이 SKKU 경로를 만들지 않게 합니다.
        ready: list[TaskNode] = []
        for task_id in sorted(self.tasks):
            node = self.tasks[task_id]
            if node.status != TaskStatus.PENDING:
                continue
            if all(self.tasks[dependency].status == TaskStatus.COMPLETED for dependency in node.depends_on):
                ready.append(node)
        return ready

    def mark_working(
        self,
        task_node_id: str,
        *,
        assigned_agent_id: str | None = None,
        a2a_task_id: str | None = None,
    ) -> None:
        node = self.require_task(task_node_id)
        node.status = TaskStatus.WORKING
        if assigned_agent_id is not None:
            node.assigned_agent_id = assigned_agent_id
        if a2a_task_id is not None:
            node.a2a_task_id = a2a_task_id

    def mark_completed(self, task_node_id: str, *, a2a_task_id: str | None = None) -> None:
        node = self.require_task(task_node_id)
        node.status = TaskStatus.COMPLETED
        node.error = None
        if a2a_task_id is not None:
            node.a2a_task_id = a2a_task_id

    def mark_failed(self, task_node_id: str, error: str, *, a2a_task_id: str | None = None) -> None:
        node = self.require_task(task_node_id)
        node.status = TaskStatus.FAILED
        node.error = error
        if a2a_task_id is not None:
            node.a2a_task_id = a2a_task_id
        self.block_dependents(task_node_id, reason=f"Dependency failed: {task_node_id}")

    def mark_input_required(self, task_node_id: str, error: str, *, a2a_task_id: str | None = None) -> None:
        node = self.require_task(task_node_id)
        node.status = TaskStatus.INPUT_REQUIRED
        node.error = error
        if a2a_task_id is not None:
            node.a2a_task_id = a2a_task_id
        self.block_dependents(task_node_id, reason=f"Dependency requires input: {task_node_id}")

    def block_dependents(self, task_node_id: str, *, reason: str) -> None:
        # 선행 작업의 실패나 입력 요청이 뒤 단계의 실행으로 이어지지 않도록 전파합니다.
        for node in self.tasks.values():
            if task_node_id in node.depends_on and node.status in {TaskStatus.PENDING, TaskStatus.WORKING}:
                node.status = TaskStatus.BLOCKED
                node.error = reason
                self.block_dependents(node.task_node_id, reason=reason)

    def require_task(self, task_node_id: str) -> TaskNode:
        try:
            return self.tasks[task_node_id]
        except KeyError as exc:
            raise TaskGraphError(f"Unknown task node id: {task_node_id}") from exc

    def all_completed(self) -> bool:
        return bool(self.tasks) and all(node.status == TaskStatus.COMPLETED for node in self.tasks.values())


class TaskGraphError(ValueError):
    """Raised when the minimal task graph is invalid or misused."""


class WorkflowStatus(StrEnum):
    """Lifecycle values for a supervisor-managed global workflow."""

    PENDING = "pending"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    INPUT_REQUIRED = "input_required"


class WorkflowRequest(BaseModel):
    """High-level user request accepted by the Phase 3 supervisor."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = "1.0"
    request_id: str | None = None
    global_task_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    workflow_mode: Literal["direct", "process_then_robot"] = "direct"
    capability: str = Field(default="robot_path_planning", min_length=1)
    agent_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    part_id: str = ""
    source: str | None = None
    destination: str | None = None
    requested_machine_id: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_mode_options(self) -> "WorkflowRequest":
        if self.workflow_mode == "process_then_robot" and not self.part_id:
            raise ValueError("공정 후 로봇 경로 모드에는 부품 ID가 필요합니다.")
        if self.workflow_mode == "process_then_robot" and self.agent_id:
            raise ValueError("기관 직접 지정은 direct 모드에서 사용하세요.")
        if self.workflow_mode == "direct" and self.metadata.get("unity_preview") is True and self.capability != "robot_path_planning":
            raise ValueError("Unity 재생은 robot_path_planning 기능에서만 사용할 수 있습니다.")
        return self


class SharedTaskState(BaseModel):
    """In-memory representation of a workflow shared across supervisor steps."""

    model_config = ConfigDict(extra="forbid")

    global_task_id: str
    user_goal: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    tasks: dict[str, TaskNode] = Field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeterministicPilotPlanner:
    """직접 실행은 한 단계, 기관 협업은 CAU→SKKU 두 단계로 고정합니다."""

    async def create_plan(self, request: WorkflowRequest) -> TaskGraph:
        graph = TaskGraph(global_task_id=request.global_task_id)
        if request.workflow_mode == "direct":
            graph.add_task(TaskNode(task_node_id="T1", capability=request.capability))
            return graph
        graph.add_task(
            TaskNode(
                task_node_id="T1",
                capability="process_planning",
                depends_on=[],
            )
        )
        graph.add_task(
            TaskNode(
                task_node_id="T2",
                capability="robot_path_planning",
                depends_on=["T1"],
            )
        )
        return graph


class InMemoryStateStore:
    """프로세스 안에서만 유지되는 상태 저장소. 재시작하면 기록이 사라집니다."""

    def __init__(self) -> None:
        self._states: dict[str, SharedTaskState] = {}
        self._lock = asyncio.Lock()

    async def create(self, state: SharedTaskState) -> SharedTaskState:
        async with self._lock:
            self._states[state.global_task_id] = _clone(state)
            return _clone(state)

    async def get(self, global_task_id: str) -> SharedTaskState | None:
        async with self._lock:
            state = self._states.get(global_task_id)
            return _clone(state) if state is not None else None

    async def update(self, state: SharedTaskState) -> SharedTaskState:
        async with self._lock:
            self._states[state.global_task_id] = _clone(state)
            return _clone(state)

    async def save_artifact(
        self,
        global_task_id: str,
        artifact_name: str,
        artifact: BaseModel | dict[str, Any],
    ) -> SharedTaskState:
        # 외부 Agent의 구조화된 결과를 이름별로 저장하고, 호출자에게 복사본을 돌려줍니다.
        async with self._lock:
            try:
                state = self._states[global_task_id]
            except KeyError as exc:
                raise KeyError(f"Workflow state not found: {global_task_id}") from exc
            if isinstance(artifact, BaseModel):
                state.artifacts[artifact_name] = artifact.model_dump(mode="json")
            else:
                state.artifacts[artifact_name] = dict(artifact)
            self._states[global_task_id] = _clone(state)
            return _clone(state)


def _clone(state: SharedTaskState) -> SharedTaskState:
    return state.model_copy(deep=True)
