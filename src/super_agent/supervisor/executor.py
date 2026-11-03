"""기관별 A2A 호출을 순서대로 실행하고 결과·실패를 공통 작업 상태에 반영합니다."""

from __future__ import annotations

import json
import asyncio
from typing import Any, Callable, cast

from pydantic import BaseModel, ValidationError

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Message, Part, Task, TaskState as A2ATaskState, TaskStatus as A2AStatus

from cau.contracts import ProcessPlanArtifact, ProcessPlanningRequest
from agent_shared.a2a import dict_to_data_part, part_to_data, timestamp_now
from super_agent.supervisor.a2a_client import A2AAgentClient, A2AClientError, A2ATaskResult
from super_agent.supervisor.workflow import (
    DeterministicPilotPlanner,
    InMemoryStateStore,
    SharedTaskState,
    TaskGraph,
    TaskNode,
    TaskStatus,
    WorkflowRequest,
    WorkflowStatus,
)
from super_agent.supervisor.registry import AgentEndpoint, CapabilityRegistry
from skku.models import RobotPlanArtifact
from super_agent.supervisor.requests import (
    ProcessPlanToRobotTaskTransformer,
    TransformationInputError,
    build_direct_request,
)
from super_agent.supervisor.telemetry import TraceStore
from super_agent.supervisor.unity_preview import UnityPreviewStore, UnityPreviewError


A2AClientFactory = Callable[[AgentEndpoint], A2AAgentClient]


class SupervisorWorkflowExecutor:
    """선택한 기능 하나 또는 CAU 공정→SKKU 경로 흐름을 실행합니다."""

    def __init__(
        self,
        *,
        planner: DeterministicPilotPlanner,
        resolver: CapabilityRegistry,
        state_store: InMemoryStateStore,
        task_transformer: ProcessPlanToRobotTaskTransformer,
        a2a_client_factory: A2AClientFactory = A2AAgentClient,
        trace_store: TraceStore | None = None,
    ) -> None:
        self.planner = planner
        self.resolver = resolver
        self.state_store = state_store
        self.task_transformer = task_transformer
        self.a2a_client_factory = a2a_client_factory
        self.trace_store = trace_store if trace_store is not None else TraceStore()
        self.unity_previews = UnityPreviewStore(self.trace_store)
        self._robot_lock = asyncio.Lock()

    async def run(self, request: WorkflowRequest) -> SharedTaskState:
        self.trace_store.begin(request)
        try:
            # 공유 Twinverse 경로 상태가 겹치지 않도록 현재는 전체 작업을 직렬 실행합니다.
            async with self._robot_lock:
                state = await self._run(request)
        except BaseException as exc:
            self.trace_store.fail(request.global_task_id, str(exc) or type(exc).__name__)
            raise
        self.trace_store.finish(state)
        return state

    async def _update_state(self, state: SharedTaskState) -> SharedTaskState:
        saved = await self.state_store.update(state)
        self.trace_store.set_state(saved)
        return saved

    async def _run(self, request: WorkflowRequest) -> SharedTaskState:
        graph = await self.planner.create_plan(request)
        state = SharedTaskState(
            global_task_id=request.global_task_id,
            user_goal=request.goal,
            status=WorkflowStatus.WORKING,
            tasks=_clone_tasks(graph),
            metadata={
                "request_id": request.request_id,
                "schema_version": request.schema_version,
                "workflow_mode": request.workflow_mode,
                "capability": request.capability if request.workflow_mode == "direct" else None,
            },
        )
        state = await self.state_store.create(state)
        self.trace_store.set_state(state)

        while True:
            ready_tasks = graph.ready_tasks()
            if not ready_tasks:
                break
            for node in ready_tasks:
                state = await self._execute_node(request, graph, state, node)
                if state.status in {WorkflowStatus.FAILED, WorkflowStatus.INPUT_REQUIRED}:
                    return state

        if graph.all_completed():
            if request.metadata.get("unity_preview") is True:
                # 경로 발행과 Unity 명령 승인·로컬 재생은 별개의 완료 단계입니다.
                try:
                    plan = RobotPlanArtifact.model_validate(state.artifacts["robot_plan"])
                    self.unity_previews.start(plan)
                    preview = await self.unity_previews.wait(request.global_task_id)
                    state.artifacts["unity_preview"] = {"artifact_type": "unity_preview", **preview}
                except UnityPreviewError as exc:
                    state.status = WorkflowStatus.FAILED
                    state.errors.append({"stage": "unity_preview", "error": str(exc)})
                    state.tasks = _clone_tasks(graph)
                    return await self._update_state(state)
            state.status = WorkflowStatus.COMPLETED
        elif any(node.status == TaskStatus.INPUT_REQUIRED for node in graph.tasks.values()):
            state.status = WorkflowStatus.INPUT_REQUIRED
        else:
            state.status = WorkflowStatus.FAILED
        state.tasks = _clone_tasks(graph)
        return await self._update_state(state)

    async def _execute_node(
        self,
        request: WorkflowRequest,
        graph: TaskGraph,
        state: SharedTaskState,
        node: TaskNode,
    ) -> SharedTaskState:
        try:
            endpoint = await self.resolver.resolve(node.capability, request.agent_id) if request.agent_id else await self.resolver.resolve(node.capability)
            graph.mark_working(node.task_node_id, assigned_agent_id=endpoint.agent_id)
            state.tasks = _clone_tasks(graph)
            state = await self._update_state(state)

            payload = self._payload_for_node(request, state, node)
            self.trace_store.emit(request.global_task_id, "delegation_started", source="supervisor", target=endpoint.agent_id,
                                  task_node_id=node.task_node_id, capability=node.capability, data=payload)

            def observe(kind: str, data: dict[str, Any]) -> None:
                incoming = kind == "http_response"
                self.trace_store.emit(request.global_task_id, kind,
                                      source=endpoint.agent_id if incoming else "supervisor",
                                      target="supervisor" if incoming else endpoint.agent_id,
                                      task_node_id=node.task_node_id, capability=node.capability, data=data)

            client = self.a2a_client_factory(endpoint)
            # 실제 통신은 A2A SDK가 담당하고, 관찰 콜백은 대시보드 통신 기록에만 씁니다.
            if isinstance(client, A2AAgentClient):
                result = await client.send_structured_message(payload, observer=observe)
            else:
                result = await client.send_structured_message(payload)
            self.trace_store.emit(request.global_task_id, "task_received", source=endpoint.agent_id, target="supervisor",
                                  task_node_id=node.task_node_id, capability=node.capability, status=result.state,
                                  data=result.model_dump(mode="json"))
            graph.mark_working(node.task_node_id, assigned_agent_id=endpoint.agent_id, a2a_task_id=result.task_id)
            state.tasks = _clone_tasks(graph)
            state = await self._update_state(state)

            if result.state == "completed":
                return await self._handle_completed_result(request, graph, state, node, result)
            if result.state == "input_required":
                return await self._handle_input_required_result(graph, state, node, result)
            return await self._handle_failed_result(graph, state, node, result, f"A2A task ended in state: {result.state}")
        except (A2AClientError, TransformationInputError, LookupError, ValidationError, ValueError) as exc:
            self.trace_store.emit(request.global_task_id, "delegation_failed", source="supervisor",
                                  task_node_id=node.task_node_id, capability=node.capability, status="failed", data={"error": str(exc)})
            graph.mark_failed(node.task_node_id, str(exc))
            state.status = WorkflowStatus.FAILED
            state.tasks = _clone_tasks(graph)
            state.errors.append(
                {
                    "task_node_id": node.task_node_id,
                    "capability": node.capability,
                    "error": str(exc),
                }
            )
            return await self._update_state(state)

    def _payload_for_node(
        self,
        request: WorkflowRequest,
        state: SharedTaskState,
        node: TaskNode,
    ) -> dict[str, Any]:
        if request.workflow_mode == "direct":
            return build_direct_request(request, default_source=self.task_transformer.default_source)
        if node.capability == "process_planning":
            return ProcessPlanningRequest(
                request_id=f"REQ-PP-{request.global_task_id}",
                global_task_id=request.global_task_id,
                capability="process_planning",
                part_id=request.part_id,
                requested_machine_id=request.requested_machine_id,
                constraints=request.constraints,
            ).model_dump(mode="json")

        if node.capability == "robot_path_planning":
            # T2는 T1에서 저장한 공정 Artifact를 읽어 새 A2A 요청을 만듭니다.
            process_plan_data = state.artifacts.get("process_plan")
            if process_plan_data is None:
                raise TransformationInputError("ProcessPlanArtifact is required before robot_path_planning.")
            process_plan = ProcessPlanArtifact.model_validate(process_plan_data)
            robot_request = self.task_transformer.transform(process_plan, request)
            self.trace_store.emit(request.global_task_id, "payload_transformed", source="supervisor", target="skku_robot",
                                  task_node_id=node.task_node_id, capability=node.capability,
                                  data={"from_artifact": "process_plan", "request": robot_request.model_dump(mode="json")})
            return robot_request.model_dump(mode="json")

        raise ValueError(f"No pilot payload builder for capability: {node.capability}")

    async def _handle_completed_result(
        self,
        request: WorkflowRequest,
        graph: TaskGraph,
        state: SharedTaskState,
        node: TaskNode,
        result: A2ATaskResult,
    ) -> SharedTaskState:
        if node.capability == "process_planning":
            # 완료 상태여도 작업 ID와 Artifact 형식을 검증한 뒤 저장합니다.
            payload = _required_artifact(result, "process_plan")
            artifact = ProcessPlanArtifact.model_validate(payload)
            if artifact.global_task_id != request.global_task_id:
                raise ValueError("CAU ProcessPlanArtifact.global_task_id does not match workflow.")
            state = await self.state_store.save_artifact(request.global_task_id, "process_plan", artifact)
        elif node.capability == "robot_path_planning":
            payload = _required_artifact(result, "robot_plan")
            artifact = RobotPlanArtifact.model_validate(payload)
            if artifact.global_task_id != request.global_task_id:
                raise ValueError("SKKU RobotPlanArtifact.global_task_id does not match workflow.")
            if artifact.execution_mode != "plan_only":
                raise ValueError("SKKU RobotPlanArtifact must remain plan_only.")
            if artifact.status in {"invalid", "failed"} or artifact.validation.errors:
                raise ValueError("SKKU returned an invalid robot plan.")
            state = await self.state_store.save_artifact(request.global_task_id, "robot_plan", artifact)
        else:
            if not result.artifacts:
                raise ValueError("The selected capability returned no structured result artifact.")
            for item in result.artifacts:
                payload = item.data
                if payload.get("global_task_id", request.global_task_id) != request.global_task_id:
                    raise ValueError("Agent result global_task_id does not match workflow.")
                state = await self.state_store.save_artifact(request.global_task_id, item.name or node.capability, payload)

        graph.mark_completed(node.task_node_id, a2a_task_id=result.task_id)
        self.trace_store.emit(request.global_task_id, "artifact_saved", source=node.assigned_agent_id, target="supervisor",
                              task_node_id=node.task_node_id, capability=node.capability, status="completed", data=payload)
        if node.capability == "robot_path_planning":
            self.trace_store.emit(request.global_task_id, "plan_published", source="skku_robot", target="unity",
                                  status="published", data=payload)
        state.status = WorkflowStatus.WORKING
        state.tasks = _clone_tasks(graph)
        return await self._update_state(state)

    async def _handle_input_required_result(
        self,
        graph: TaskGraph,
        state: SharedTaskState,
        node: TaskNode,
        result: A2ATaskResult,
    ) -> SharedTaskState:
        error_artifact = result.artifact_data("error") or result.first_artifact_data() or {"state": result.state}
        graph.mark_input_required(node.task_node_id, _error_message(error_artifact), a2a_task_id=result.task_id)
        state.status = WorkflowStatus.INPUT_REQUIRED
        state.tasks = _clone_tasks(graph)
        state.errors.append(
            {
                "task_node_id": node.task_node_id,
                "capability": node.capability,
                "error": _error_message(error_artifact),
            }
        )
        state = await self.state_store.save_artifact(state.global_task_id, "error", error_artifact)
        return await self._update_state(state)

    async def _handle_failed_result(
        self,
        graph: TaskGraph,
        state: SharedTaskState,
        node: TaskNode,
        result: A2ATaskResult,
        message: str,
    ) -> SharedTaskState:
        error_artifact = result.artifact_data("error") or result.first_artifact_data() or {"message": message}
        graph.mark_failed(node.task_node_id, _error_message(error_artifact), a2a_task_id=result.task_id)
        state.status = WorkflowStatus.FAILED
        state.tasks = _clone_tasks(graph)
        state.errors.append(
            {
                "task_node_id": node.task_node_id,
                "capability": node.capability,
                "error": _error_message(error_artifact),
            }
        )
        state = await self.state_store.save_artifact(state.global_task_id, "error", error_artifact)
        return await self._update_state(state)


class SupervisorA2AExecutor(AgentExecutor):
    """Expose the supervisor workflow as an A2A agent."""

    def __init__(self, workflow_executor: SupervisorWorkflowExecutor) -> None:
        self.workflow_executor = workflow_executor

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = cast(str, context.task_id)
        context_id = cast(str, context.context_id)
        await event_queue.enqueue_event(self._initial_task(context, task_id, context_id))
        updater = TaskUpdater(event_queue, task_id, context_id)
        payload: dict[str, Any] = {}

        try:
            payload = self._extract_payload(context.message)
            request = WorkflowRequest.model_validate(payload)
            await updater.start_work(self._agent_text(updater, "Supervisor workflow started."))
            state = await self.workflow_executor.run(request)
            await updater.add_artifact(
                [dict_to_data_part(state.model_dump(mode="json"))],
                artifact_id=state.global_task_id,
                name="shared_task_state",
                metadata={"global_task_id": state.global_task_id, "workflow_status": state.status.value},
                last_chunk=True,
            )
            if state.status == WorkflowStatus.COMPLETED:
                await updater.complete(self._agent_text(updater, "Supervisor workflow completed."))
            elif state.status == WorkflowStatus.INPUT_REQUIRED:
                await updater.requires_input(self._agent_text(updater, "Supervisor workflow requires input."))
            else:
                await updater.failed(self._agent_text(updater, "Supervisor workflow failed."))
        except ValidationError as exc:
            details = [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]
            await self._publish_error(updater, "invalid_workflow_request", "WorkflowRequest payload is invalid.", details, payload)
            await updater.requires_input(self._agent_text(updater, "WorkflowRequest payload is invalid."))
        except Exception as exc:
            await self._publish_error(updater, "supervisor_failed", str(exc), [], payload)
            await updater.failed(self._agent_text(updater, f"Supervisor failed: {exc}"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = cast(str, context.task_id)
        context_id = cast(str, context.context_id)
        updater = TaskUpdater(event_queue, task_id, context_id)
        await updater.cancel(self._agent_text(updater, "Supervisor workflow was canceled."))

    def _initial_task(self, context: RequestContext, task_id: str, context_id: str) -> Task:
        history = [context.message] if context.message is not None else []
        return Task(
            id=task_id,
            context_id=context_id,
            status=A2AStatus(
                state=A2ATaskState.TASK_STATE_SUBMITTED,
                timestamp=timestamp_now(),
            ),
            history=history,
        )

    def _extract_payload(self, message: Message | None) -> dict[str, Any]:
        if message is None:
            raise ValueError("A2A message is required.")

        text_parts: list[str] = []
        for part in message.parts:
            data = part_to_data(part)
            if data is not None:
                return data
            if part.WhichOneof("content") == "text":
                text_parts.append(part.text)

        if not text_parts:
            raise ValueError("A2A message must contain a data part or JSON text.")
        payload = json.loads("\n".join(text_parts))
        if not isinstance(payload, dict):
            raise ValueError("Workflow payload must be a JSON object.")
        return payload

    async def _publish_error(
        self,
        updater: TaskUpdater,
        error_code: str,
        message: str,
        details: list[str],
        payload: dict[str, Any],
    ) -> None:
        error = {
            "artifact_type": "error",
            "global_task_id": payload.get("global_task_id"),
            "error_code": error_code,
            "message": message,
            "details": details,
        }
        await updater.add_artifact(
            [dict_to_data_part(error)],
            artifact_id=f"error-{error_code}",
            name="error",
            last_chunk=True,
        )

    @staticmethod
    def _agent_text(updater: TaskUpdater, text: str) -> Message:
        return updater.new_agent_message([Part(text=text)], metadata={"source": "supervisor"})


def _clone_tasks(graph: TaskGraph) -> dict[str, TaskNode]:
    return {task_id: node.model_copy(deep=True) for task_id, node in graph.tasks.items()}


def _required_artifact(result: A2ATaskResult, name: str) -> dict[str, Any]:
    payload = result.artifact_data(name)
    if payload is None:
        raise ValueError(f"A2A task {result.task_id} did not return required artifact: {name}")
    return payload


def _error_message(error_artifact: dict[str, Any]) -> str:
    message = error_artifact.get("message")
    if isinstance(message, str) and message:
        return message
    return "A2A task failed."
