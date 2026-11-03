"""Standalone A2A app for the Phase 2 SKKU Twinverse robot agent."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from contextlib import asynccontextmanager
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

from agent_shared.config import AgentConfig, load_config
from skku.backend import RobotBackend
from skku.backends.twinverse_mcp import TwinverseMcpBackend
from skku.executor import SkkuA2AExecutor


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
        provider=AgentProvider(organization="SKKU SDML", url="https://github.com/skku-sdml/twinverse"),
        version="0.2.0",
        capabilities=AgentCapabilities(streaming=agent.a2a.streaming, push_notifications=False),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="robot_path_planning",
                name="Robot path planning",
                description="Plan and validate a Twinverse robot path without starting robot motion.",
                tags=["robot", "planning", "twinverse", "plan-only"],
                examples=["Plan moving Part_A from InputStation to Machine_01."],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
            AgentSkill(
                id="robot_state_query",
                name="Robot state query",
                description="Query Twinverse robot and scene state through safe MCP tools.",
                tags=["robot", "state", "twinverse"],
                examples=["Return the current robot planning state."],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
            AgentSkill(
                id="path_validation",
                name="Path validation",
                description="Validate a plan-only robot request against SKKU safety constraints.",
                tags=["robot", "validation", "safety"],
                examples=["Validate the planned path for TASK-001."],
                input_modes=["application/json"],
                output_modes=["application/json"],
            ),
        ],
    )


def create_skku_app(agent: AgentConfig | None = None, backend: RobotBackend | None = None) -> Starlette:
    # A2A 서버는 외부 요청을 받고, 업무 동작은 별도 Twinverse MCP 백엔드에 맡깁니다.
    if agent is None:
        agent = load_config().require_agent("skku_robot")
    if backend is None:
        if agent.mcp is None:
            raise ValueError("SKKU robot agent requires an MCP endpoint.")
        backend = TwinverseMcpBackend.from_config(agent.mcp)

    executor = SkkuA2AExecutor(backend)
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
                "mcp": str(agent.mcp.url) if agent.mcp and agent.mcp.url else None,
                "execution_mode": "plan_only",
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
    parser = argparse.ArgumentParser(description="Run the standalone SKKU Robot A2A agent.")
    parser.add_argument("--config", default=None, help="Path to agents YAML file.")
    parser.add_argument("--env-file", default=None, help="Path to .env file.")
    parser.add_argument("--host", default=None, help="Override SKKU A2A host.")
    parser.add_argument("--port", type=int, default=None, help="Override SKKU A2A port.")
    args = parser.parse_args(argv)

    config = load_config(args.config, args.env_file)
    agent = config.require_agent("skku_robot")
    host = args.host or agent.a2a.host
    port = args.port or agent.a2a.port

    import uvicorn

    uvicorn.run(
        create_skku_app(agent),
        host=host,
        port=port,
        log_level=config.supervisor.log_level.lower(),
    )


if __name__ == "__main__":
    main()
