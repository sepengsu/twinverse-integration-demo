"""샘플 YAML로 공정 결과를 재현하는 CAU 더미 백엔드입니다."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from cau.contracts import (
    EquipmentCandidate,
    EquipmentSelectionArtifact,
    EquipmentSelectionRequest,
    OperationStep,
    ProcessInformationArtifact,
    ProcessInformationRequest,
    ProcessPlanArtifact,
    ProcessPlanningRequest,
)


class DummyCauBackend:
    """실제 설비에 연결하지 않고 요청 ID별로 같은 샘플 결과를 반환합니다."""

    def __init__(self, fixture_file: Path) -> None:
        self.fixture_file = fixture_file
        self._fixture = self._load_fixture(fixture_file)
        self._cache: dict[tuple[str, str], BaseModel] = {}
        self._lock = asyncio.Lock()

    async def create_process_plan(self, request: ProcessPlanningRequest) -> ProcessPlanArtifact:
        # 동일 요청을 다시 받으면 메모리의 기존 결과를 사용합니다(재시작 시 초기화).
        cache_key = ("process_planning", request.request_id or request.global_task_id)
        async with self._lock:
            cached = self._cache.get(cache_key)
            if isinstance(cached, ProcessPlanArtifact):
                return cached

            process_plan = self._fixture.get("process_plan", {})
            operations = []
            machine_id = request.requested_machine_id or "Machine_01"
            for index, item in enumerate(process_plan.get("operations", []), start=1):
                data = dict(item)
                data["sequence"] = index
                data["part_id"] = request.part_id
                # 요청한 설비가 있으면 모든 단계의 자원 ID에도 같은 값을 적용합니다.
                data["resource_id"] = machine_id if request.requested_machine_id else data.get("resource_id") or machine_id
                operations.append(OperationStep.model_validate(data))

            artifact = ProcessPlanArtifact(
                schema_version=str(process_plan.get("version", "1.0")),
                request_id=request.request_id,
                global_task_id=request.global_task_id,
                plan_id=str(process_plan.get("plan_id", "PP-001")),
                part_id=request.part_id,
                machine_id=machine_id,
                operations=operations,
            )
            self._cache[cache_key] = artifact
            return artifact

    async def select_equipment(self, request: EquipmentSelectionRequest) -> EquipmentSelectionArtifact:
        cache_key = ("equipment_selection", request.request_id or request.global_task_id)
        async with self._lock:
            cached = self._cache.get(cache_key)
            if isinstance(cached, EquipmentSelectionArtifact):
                return cached

            equipment = self._fixture.get("equipment", {})
            candidates = [
                EquipmentCandidate.model_validate(item)
                for item in equipment.get("selected_equipment", [])
            ]
            artifact = EquipmentSelectionArtifact(
                request_id=request.request_id,
                global_task_id=request.global_task_id,
                selection_id=str(equipment.get("selection_id", "ES-001")),
                selected_equipment=candidates,
            )
            self._cache[cache_key] = artifact
            return artifact

    async def get_process_information(self, request: ProcessInformationRequest) -> ProcessInformationArtifact:
        cache_key = ("process_information", request.request_id or request.global_task_id)
        async with self._lock:
            cached = self._cache.get(cache_key)
            if isinstance(cached, ProcessInformationArtifact):
                return cached

            info = self._fixture.get("process_information", {})
            artifact = ProcessInformationArtifact(
                request_id=request.request_id,
                global_task_id=request.global_task_id,
                information_id=str(info.get("information_id", "PI-001")),
                supported_capabilities=[
                    "process_planning",
                    "equipment_selection",
                    "process_information",
                ],
                supported_processes=list(info.get("supported_processes", [])),
                supported_materials=list(info.get("supported_materials", [])),
                notes=list(info.get("notes", [])),
            )
            self._cache[cache_key] = artifact
            return artifact

    @staticmethod
    def _load_fixture(fixture_file: Path) -> dict[str, Any]:
        if not fixture_file.is_file():
            raise FileNotFoundError(f"CAU dummy fixture not found: {fixture_file}")
        with fixture_file.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
        if not isinstance(data, dict):
            raise ValueError(f"CAU dummy fixture root must be a mapping: {fixture_file}")
        return data
