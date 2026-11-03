"""웹 UI, 비동기 작업 시작, 기관 연결 확인, 실시간 기록을 제공합니다."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from super_agent.supervisor.workflow import WorkflowRequest
from agent_shared.config import AppConfig
from super_agent.supervisor.registry import CapabilityRegistry
from super_agent.supervisor.executor import SupervisorWorkflowExecutor
from super_agent.supervisor.telemetry import _redact
from super_agent.supervisor.unity_preview import UnityPreviewError


WEB_ROOT = Path(__file__).resolve().parents[2] / "ui"


class Dashboard:
    def __init__(self, config: AppConfig, registry: CapabilityRegistry, executor: SupervisorWorkflowExecutor) -> None:
        self.config = config
        self.registry = registry
        self.executor = executor
        self.traces = executor.trace_store
        self.running: dict[str, asyncio.Task[None]] = {}
        self.kafka_events = None

    async def page(self, _: Request) -> FileResponse:
        return FileResponse(WEB_ROOT / "index.html", headers={"Cache-Control": "no-cache"})

    async def snapshot(self, _: Request) -> JSONResponse:
        return JSONResponse({**self.traces.snapshot(), "unity_runtime": self.executor.unity_previews.health(),
                             "kafka_events":self.kafka_events.snapshot() if self.kafka_events else {"enabled":False}}, headers={"Cache-Control": "no-store"})

    async def kafka_status(self, _: Request) -> JSONResponse:
        return JSONResponse(self.kafka_events.snapshot(include_records=True) if self.kafka_events else {"enabled":False},
                            headers={"Cache-Control":"no-store"})

    @staticmethod
    def _local_runtime(request: Request) -> bool:
        # Unity 브리지는 현재 Supervisor와 같은 PC에서만 호출하도록 제한합니다.
        return request.client is not None and request.client.host in {"127.0.0.1", "::1"} and not request.headers.get("origin")

    async def unity_job(self, request: Request) -> JSONResponse:
        if not self._local_runtime(request):
            return JSONResponse({"error": "local_runtime_required"}, status_code=403)
        self.executor.unity_previews.heartbeat(request.query_params.get("scene", "Unity"))
        return JSONResponse({"job": self.executor.unity_previews.next_job(), "session_id": self.executor.unity_previews.session_id}, headers={"Cache-Control": "no-store"})

    async def unity_report(self, request: Request) -> JSONResponse:
        if not self._local_runtime(request):
            return JSONResponse({"error": "local_runtime_required"}, status_code=403)
        try:
            result = self.executor.unity_previews.report(await request.json())
        except (UnityPreviewError, ValueError, TypeError, AttributeError) as exc:
            return JSONResponse({"error": "invalid_unity_report", "message": str(exc)}, status_code=400)
        self.executor.unity_previews.heartbeat("SKKU_RobotRoom")
        return JSONResponse(result)

    async def agents(self, request: Request) -> JSONResponse:
        async def probe(registration: Any) -> dict[str, Any]:
            agent = self.config.require_agent(registration.agent_id)
            url = registration.endpoint.base_url.rstrip("/")
            parsed = urlsplit(url)
            origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
            row: dict[str, Any] = {
                "id": registration.agent_id, "name": registration.name,
                "url": url, "capabilities": registration.capabilities,
                "mode": agent.mode, "enabled": registration.enabled,
                "status": "disabled", "latency_ms": None, "card": None, "error": None,
            }
            if not registration.enabled:
                return row
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=2.5) as client:
                    health, card = await asyncio.gather(
                        client.get(f"{origin}/health"),
                        client.get(f"{origin}/.well-known/agent-card.json"),
                    )
                health.raise_for_status()
                card.raise_for_status()
                row.update(status="online", latency_ms=round((time.monotonic() - started) * 1000), card=_redact(card.json()))
            except Exception as exc:
                row.update(status="offline", error=str(exc) or type(exc).__name__)
            return row

        rows = await asyncio.gather(*(probe(item) for item in self.registry.registrations()))
        rows.insert(0, {
            "id": "supervisor", "name": self.config.supervisor.name,
            "url": str(request.base_url).rstrip("/"), "capabilities": ["direct_agent_task", "manufacturing_pilot_workflow"],
            "mode": "orchestrator", "enabled": True, "status": "online", "latency_ms": 0,
            "card": None, "error": None,
        })
        return JSONResponse({"agents": rows}, headers={"Cache-Control": "no-store"})

    async def launch(self, request: Request) -> JSONResponse:
        # 대시보드에서 연 서버 자신에게만 작업을 제출합니다.
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse({"error": "cross_origin_request"}, status_code=403)
        try:
            workflow = WorkflowRequest.model_validate(await request.json())
            if not workflow.global_task_id or not workflow.goal:
                raise ValueError("작업 ID와 목표를 입력하세요.")
        except ValidationError as exc:
            return JSONResponse({"error": "invalid_workflow_request", "details": json.loads(exc.json())}, status_code=422)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": "invalid_request", "message": str(exc)}, status_code=400)

        task_id = workflow.global_task_id
        if task_id in self.running or task_id in self.traces.workflows or await self.executor.state_store.get(task_id) is not None:
            return JSONResponse({"error": "workflow_already_exists", "global_task_id": task_id}, status_code=409)
        active_ids = {key for key, row in self.traces.workflows.items() if row["status"] == "working"} | set(self.running)
        if len(active_ids) >= self.traces.max_workflows:
            return JSONResponse({"error": "too_many_active_workflows"}, status_code=429)

        async def execute() -> None:
            # HTTP 응답은 즉시 반환하고, 실제 A2A 호출은 백그라운드 작업에서 진행합니다.
            try:
                await self.executor.run(workflow)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # run()이 이미 남긴 실패 기록을 중복 생성하지 않습니다.
                if task_id not in self.traces.workflows:
                    self.traces.fail(task_id, str(exc) or type(exc).__name__)
            finally:
                self.running.pop(task_id, None)

        self.running[task_id] = asyncio.create_task(execute(), name=f"dashboard-{task_id}")
        return JSONResponse({"global_task_id": task_id, "status": "accepted"}, status_code=202)

    async def events(self, request: Request) -> StreamingResponse:
        try:
            cursor = max(0, int(request.headers.get("last-event-id") or request.query_params.get("after", "0")))
        except ValueError:
            return JSONResponse({"error": "invalid_cursor"}, status_code=400)

        async def stream():
            nonlocal cursor
            heartbeat = time.monotonic()
            yield "retry: 2000\n\n"
            if cursor > self.traces.cursor:
                # 재시작 후 이벤트 번호가 초기화되므로 이전 커서는 버립니다.
                cursor = 0
                yield 'id: 0\ndata: {"kind": "reset"}\n\n'
            while not await request.is_disconnected():
                for event in self.traces.events_since(cursor):
                    cursor = event["id"]
                    # Payloads are fetched through snapshot so a reconnect can recover a complete view.
                    notice = {key: event[key] for key in ("id", "global_task_id", "kind")}
                    yield f"id: {cursor}\ndata: {json.dumps(notice, ensure_ascii=False)}\n\n"
                if time.monotonic() - heartbeat >= 10:
                    heartbeat = time.monotonic()
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def close(self) -> None:
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def routes(self) -> list:
        return [
            Route("/", self.page),
            Route("/dashboard", self.page),
            Route("/dashboard/api/snapshot", self.snapshot),
            Route("/dashboard/api/kafka", self.kafka_status),
            Route("/dashboard/api/agents", self.agents),
            Route("/dashboard/api/workflows", self.launch, methods=["POST"]),
            Route("/dashboard/api/events", self.events),
            Route("/dashboard/api/unity/job", self.unity_job),
            Route("/dashboard/api/unity/report", self.unity_report, methods=["POST"]),
            Mount("/dashboard/assets", StaticFiles(directory=WEB_ROOT), name="dashboard-assets"),
        ]
