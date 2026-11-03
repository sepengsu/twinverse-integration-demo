"""Supervisor의 A2A 서버, 작업 API, 대시보드를 함께 구성합니다."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import AsyncIterator

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol

from super_agent.supervisor.workflow import DeterministicPilotPlanner, InMemoryStateStore, WorkflowRequest
from agent_shared.config import AppConfig, SupervisorConfig, load_config
from super_agent.supervisor.registry import CapabilityRegistry
from super_agent.supervisor.executor import SupervisorA2AExecutor, SupervisorWorkflowExecutor
from super_agent.supervisor.requests import ProcessPlanToRobotTaskTransformer
from super_agent.supervisor.dashboard import Dashboard
from super_agent.supervisor.telemetry import WorkflowAlreadyExists
from super_agent.supervisor.kafka_events import KafkaEventBridge


def build_agent_card(supervisor: SupervisorConfig) -> AgentCard:
    base_url = f"http://{supervisor.host}:{supervisor.port}"
    return AgentCard(
        name=supervisor.name,
        description="Delegate one selected agent skill by default, or run an optional process-to-robot workflow.",
        supported_interfaces=[
            AgentInterface(
                url=base_url,
                protocol_binding=TransportProtocol.HTTP_JSON.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        provider=AgentProvider(organization="Super Agent", url="https://example.local/super-agent"),
        version="0.4.0",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="direct_agent_task",
                name="Direct agent skill execution",
                description="Execute a selected registered agent capability without an upstream process plan. This is the default workflow mode.",
                tags=["direct", "a2a", "supervisor"],
                examples=["Query robot state through skku_robot.", "Request process information through cau_process."],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
            AgentSkill(
                id="manufacturing_pilot_workflow",
                name="Manufacturing pilot workflow",
                description="Optional process_then_robot mode: create a CAU process plan followed by a SKKU plan-only robot handling plan.",
                tags=["manufacturing", "workflow", "a2a", "supervisor"],
                examples=["Generate a process plan for Part_A and create the required robot handling plan."],
                input_modes=["application/json"],
                output_modes=["application/json"],
            )
        ],
    )


def create_supervisor_app(
    config: AppConfig | None = None,
    *,
    workflow_executor: SupervisorWorkflowExecutor | None = None,
    state_store: InMemoryStateStore | None = None,
) -> Starlette:
    # 설정의 Agent 목록에서 기능별 주소를 만들고, 실행·기록·웹 화면을 연결합니다.
    if config is None:
        config = load_config()

    registry = CapabilityRegistry.from_config(config)
    if state_store is None and workflow_executor is not None:
        state_store = workflow_executor.state_store
    if state_store is None:
        state_store = InMemoryStateStore()
    if workflow_executor is None:
        workflow_executor = SupervisorWorkflowExecutor(
            planner=DeterministicPilotPlanner(),
            resolver=registry,
            state_store=state_store,
            task_transformer=ProcessPlanToRobotTaskTransformer(default_source=config.pilot.default_source),
        )

    kafka_events = KafkaEventBridge(config.kafka_events)
    if config.kafka_events.enabled:
        # Kafka는 기록 이벤트의 복사 대상이며 하위 Agent 호출 경로를 바꾸지 않습니다.
        workflow_executor.trace_store.event_sink = kafka_events.submit
    dashboard = Dashboard(config, registry, workflow_executor)
    dashboard.kafka_events = kafka_events

    agent_card = build_agent_card(config.supervisor)
    a2a_handler = DefaultRequestHandlerV2(
        agent_executor=SupervisorA2AExecutor(workflow_executor),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "agent": config.supervisor.name,
                "capabilities": ["direct_agent_task", "manufacturing_pilot_workflow"],
                "registry": registry.capability_index(),
                "pilot": {"default_source": config.pilot.default_source},
                "kafka_events": kafka_events.snapshot(),
                "llm": {
                    "supervisor_agent_model": config.llm.supervisor_agent_model,
                    "supervisor_agent_kwargs": config.llm.supervisor_agent_kwargs,
                },
            }
        )

    async def create_workflow(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            workflow_request = WorkflowRequest.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse({"error": "invalid_workflow_request", "details": exc.errors()}, status_code=422)
        except Exception as exc:
            return JSONResponse({"error": "invalid_json", "message": str(exc)}, status_code=400)

        try:
            state = await workflow_executor.run(workflow_request)
        except WorkflowAlreadyExists as exc:
            return JSONResponse({"error": "workflow_already_exists", "message": str(exc)}, status_code=409)
        return JSONResponse(state.model_dump(mode="json"))

    async def get_workflow(request: Request) -> JSONResponse:
        global_task_id = request.path_params["global_task_id"]
        state = await state_store.get(global_task_id)
        if state is None:
            return JSONResponse({"error": "workflow_not_found", "global_task_id": global_task_id}, status_code=404)
        return JSONResponse(state.model_dump(mode="json"))

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        await kafka_events.start()
        try:
            yield
        finally:
            await dashboard.close()
            await kafka_events.close()
            await a2a_handler.aclose()

    routes = [
        *dashboard.routes(),
        Route("/health", health, methods=["GET"]),
        Route("/workflow", create_workflow, methods=["POST"]),
        Route("/workflow/{global_task_id:str}", get_workflow, methods=["GET"]),
        *create_agent_card_routes(agent_card),
        *create_rest_routes(a2a_handler),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.dashboard = dashboard
    app.state.kafka_events = kafka_events
    return app


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Phase 3 Supervisor A2A/HTTP agent.")
    parser.add_argument("--config", default=None, help="Path to agents YAML file.")
    parser.add_argument("--env-file", default=None, help="Path to .env file.")
    parser.add_argument("--host", default=None, help="Override supervisor host.")
    parser.add_argument("--port", type=int, default=None, help="Override supervisor port.")
    args = parser.parse_args(argv)

    config = load_config(args.config, args.env_file)
    host = args.host or config.supervisor.host
    port = args.port or config.supervisor.port

    import uvicorn

    uvicorn.run(
        create_supervisor_app(config),
        host=host,
        port=port,
        log_level=config.supervisor.log_level.lower(),
    )


if __name__ == "__main__":
    main()
