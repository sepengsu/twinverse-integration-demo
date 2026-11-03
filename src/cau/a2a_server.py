"""Standalone A2A server for the Phase 1 Dummy CAU agent."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from collections.abc import Sequence
from typing import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol

from agent_shared.config import AgentConfig, PROJECT_ROOT, load_config
from cau.a2a_executor import CauA2AExecutor
from cau.dummy_backend import DummyCauBackend


def build_agent_card(agent: AgentConfig) -> AgentCard:
    interface_url = agent.a2a.interface_url()
    return AgentCard(
        name=agent.name,
        description=agent.description,
        supported_interfaces=[
            AgentInterface(
                url=interface_url,
                protocol_binding=TransportProtocol.HTTP_JSON.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        provider=AgentProvider(organization="CAU", url="https://www.cau.ac.kr"),
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=agent.a2a.streaming, push_notifications=False),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="process_planning",
                name="Process planning",
                description="Create a deterministic dummy process plan for a part.",
                tags=["manufacturing", "planning", "dummy"],
                examples=["Create a process plan for Part_A on Machine_01."],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
            AgentSkill(
                id="equipment_selection",
                name="Equipment selection",
                description="Select deterministic dummy equipment for a manufacturing process.",
                tags=["manufacturing", "equipment", "dummy"],
                examples=["Select equipment for machining Part_A."],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
            AgentSkill(
                id="process_information",
                name="Process information",
                description="Describe the dummy CAU process-agent capabilities.",
                tags=["manufacturing", "metadata", "dummy"],
                examples=["Return supported process information."],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
        ],
    )


def create_cau_app(agent: AgentConfig | None = None) -> Starlette:
    # 현재 CAU 서버는 YAML 샘플 백엔드를 사용합니다. 실제 기관 연동 시 이 지점을 교체합니다.
    if agent is None:
        agent = load_config().require_agent("cau_process")

    backend = DummyCauBackend(agent.dummy.resolved_fixture_file(PROJECT_ROOT))
    executor = CauA2AExecutor(backend)
    agent_card = build_agent_card(agent)
    handler = DefaultRequestHandlerV2(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "agent": agent.name,
                "capabilities": agent.capabilities,
            }
        )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await handler.aclose()

    routes = [
        Route("/health", health, methods=["GET"]),
        *create_agent_card_routes(agent_card),
        *create_rest_routes(handler, path_prefix=agent.a2a.path_prefix),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the standalone Dummy CAU A2A agent.")
    parser.add_argument("--config", default=None, help="Path to agents YAML file.")
    parser.add_argument("--env-file", default=None, help="Path to .env file.")
    parser.add_argument("--host", default=None, help="Override CAU A2A host.")
    parser.add_argument("--port", type=int, default=None, help="Override CAU A2A port.")
    args = parser.parse_args(argv)

    config = load_config(args.config, args.env_file)
    agent = config.require_agent("cau_process")
    host = args.host or agent.a2a.host
    port = args.port or agent.a2a.port

    import uvicorn

    uvicorn.run(
        create_cau_app(agent),
        host=host,
        port=port,
        log_level=config.supervisor.log_level.lower(),
    )


if __name__ == "__main__":
    main()
