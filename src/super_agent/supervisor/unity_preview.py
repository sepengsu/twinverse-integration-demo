"""A2A 경로 발행과 별도로 Unity의 작업별 승인·재생 결과를 확인합니다."""
from __future__ import annotations

import asyncio
import copy
import math
import secrets
import time
from collections import OrderedDict
from typing import Any

from skku.models import RobotPlanArtifact
from super_agent.supervisor.telemetry import TraceStore


class UnityPreviewError(ValueError):
    pass


class UnityPreviewStore:
    def __init__(self, traces: TraceStore, *, timeout_seconds: float = 90, max_jobs: int = 100):
        self.traces = traces
        self.timeout_seconds = timeout_seconds
        self.max_jobs = max_jobs
        self.jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._published_progress: dict[str, float] = {}
        self.heartbeat_at = 0.0
        self.scene = ""
        self.session_id = secrets.token_hex(12)

    def heartbeat(self, scene: str) -> None:
        self.heartbeat_at = time.monotonic()
        self.scene = scene[:100]

    def health(self) -> dict[str, Any]:
        return {"connected": time.monotonic() - self.heartbeat_at < 6, "scene": self.scene}

    def start(self, plan: RobotPlanArtifact) -> dict[str, Any]:
        # 경로 초기화 1건과 각 타깃의 명령 ID가 모두 고유해야 이번 작업을 추적할 수 있습니다.
        ids = [result.data["command_id"] for result in plan.mcp_results if result.data.get("command_id")]
        if len(ids) != 1 + len(plan.targets) or len(set(ids)) != len(ids):
            raise UnityPreviewError("Each path reset and waypoint requires a distinct MQTT command ID.")
        while len(self.jobs) >= self.max_jobs:
            finished = next((key for key, row in self.jobs.items() if row["status"] in {"completed", "failed"}), None)
            if finished is None:
                raise UnityPreviewError("Unity preview queue is full.")
            del self.jobs[finished]
            self._published_progress.pop(finished, None)
        job = {
            "global_task_id": plan.global_task_id, "path_id": plan.path.path_id,
            "object_id": plan.mcp_results[0].data.get("object_id", "skku/gp25_target"),
            "source": plan.source, "destination": plan.destination,
            "expected_command_ids": ids, "expected_waypoint_count": len(plan.targets),
            "report_key": secrets.token_urlsafe(24), "status": "pending", "progress": 0.0,
            "accepted_command_ids": [], "waypoints_world": [], "waypoint_count": 0,
            "playback_kind": "", "message": "Unity의 작업별 명령 승인을 기다립니다.",
            "deadline": time.monotonic() + self.timeout_seconds,
        }
        self.jobs[plan.global_task_id] = job
        self._publish(job, "unity_waiting")
        return self.public(job)

    def next_job(self) -> dict[str, Any] | None:
        job = next((row for row in self.jobs.values() if row["status"] == "pending"), None)
        return copy.deepcopy({k: v for k, v in job.items() if k != "deadline"}) if job else None

    @staticmethod
    def public(job: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy({k: v for k, v in job.items() if k not in {"deadline", "report_key"}})

    def report(self, payload: dict[str, Any]) -> dict[str, Any]:
        # 보고 키와 이번 작업의 명령·경유점·단계 순서를 모두 검증합니다.
        job = self.jobs.get(payload.get("global_task_id"))
        if job is None:
            raise UnityPreviewError("Unknown Unity preview task.")
        if not secrets.compare_digest(str(payload.get("report_key", "")), job["report_key"]):
            raise UnityPreviewError("Unity preview report key does not match.")
        if job["status"] in {"completed", "failed"}:
            raise UnityPreviewError("Unity preview already ended.")
        status = payload.get("status")
        if status not in {"accepted", "running", "completed", "failed"}:
            raise UnityPreviewError("Invalid Unity preview stage.")
        progress = payload.get("progress", 0)
        if not isinstance(progress, (int, float)) or isinstance(progress, bool) or not math.isfinite(progress) or not 0 <= progress <= 1:
            raise UnityPreviewError("Unity progress must be finite and between 0 and 1.")
        ids = payload.get("accepted_command_ids", [])
        points = payload.get("waypoints_world", [])
        if status != "failed":
            if not isinstance(ids, list) or len(ids) != len(job["expected_command_ids"]) or set(ids) != set(job["expected_command_ids"]):
                raise UnityPreviewError("Unity must confirm this task's exact MQTT command IDs.")
            if not isinstance(points, list) or len(points) != job["expected_waypoint_count"]:
                raise UnityPreviewError("Unity waypoint count does not match the published plan.")
            if any(not isinstance(point, dict) or any(not isinstance(point.get(axis), (int, float)) or not math.isfinite(point[axis]) for axis in ("x", "y", "z")) for point in points):
                raise UnityPreviewError("Unity waypoint coordinates must be finite.")
            if status == "accepted" and job["status"] != "pending":
                raise UnityPreviewError("Cannot return to the acceptance stage.")
            if status in {"running", "completed"} and job["status"] not in {"accepted", "running"}:
                raise UnityPreviewError("Unity must confirm acceptance before playback.")
            if status == "completed" and job["status"] != "running":
                raise UnityPreviewError("Unity playback must start before completion.")
            if progress < job["progress"] or status == "completed" and progress != 1:
                raise UnityPreviewError("Unity playback progress must advance and finish at 1.")
        previous_status = job["status"]
        job.update(status=status, progress=float(progress), accepted_command_ids=ids,
                   waypoints_world=points, waypoint_count=len(points),
                   playback_kind=str(payload.get("playback_kind", "tcp_path_preview"))[:80],
                   message=str(payload.get("message", ""))[:500])
        if status != previous_status or progress >= self._published_progress.get(job["global_task_id"], 0) + .04:
            self._publish(job, "unity_" + status)
        return self.public(job)

    async def wait(self, task_id: str) -> dict[str, Any]:
        # Unity의 완료 보고나 제한 시간 만료까지 기다립니다.
        job = self.jobs[task_id]
        while job["status"] not in {"completed", "failed"}:
            if time.monotonic() >= job["deadline"]:
                job.update(status="failed", message="Unity 확인 또는 경로 재생 시간이 초과됐습니다.")
                self._publish(job, "unity_failed")
                break
            await asyncio.sleep(.1)
        if job["status"] != "completed":
            raise UnityPreviewError(job["message"])
        return self.public(job)

    def _publish(self, job: dict[str, Any], kind: str) -> None:
        self._published_progress[job["global_task_id"]] = job["progress"]
        public = self.public(job)
        row = self.traces.workflows.get(job["global_task_id"])
        if row is not None:
            row["unity"] = public
        self.traces.emit(job["global_task_id"], kind, source="unity", target="supervisor",
                         status=job["status"], data=public)
