"""CAU에 보내는 요청과 CAU가 반환하는 공정 Artifact의 형식입니다."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Capability = Literal["process_planning", "equipment_selection", "process_information"]


class CauContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CauRequest(CauContract):
    # 두 ID를 유지해 한 번의 전체 작업 안에서 기관별 요청을 추적합니다.
    schema_version: str = "1.0"
    request_id: str
    global_task_id: str
    capability: Capability


class ProcessPlanningRequest(CauRequest):
    capability: Literal["process_planning"] = "process_planning"
    part_id: str = "Part_A"
    material: str | None = None
    quantity: int = Field(default=1, ge=1)
    requested_machine_id: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def pick_machine_from_constraints(self) -> "ProcessPlanningRequest":
        # 예전 입력 형식의 constraints.machine_id도 설비 지정으로 받아들입니다.
        machine_id = self.constraints.get("machine_id")
        if self.requested_machine_id is None and isinstance(machine_id, str):
            self.requested_machine_id = machine_id
        return self


class OperationStep(CauContract):
    operation_id: str
    name: str
    sequence: int = Field(ge=1)
    part_id: str
    resource_id: str
    description: str = ""
    estimated_duration_seconds: int = Field(ge=0)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ProcessPlanArtifact(CauContract):
    # machine_id는 Supervisor가 SKKU 요청의 destination으로 넘기는 값입니다.
    schema_version: str = "1.0"
    artifact_type: Literal["process_plan"] = "process_plan"
    request_id: str
    global_task_id: str
    plan_id: str
    part_id: str
    machine_id: str
    operations: list[OperationStep]
    deterministic: bool = True


class EquipmentSelectionRequest(CauRequest):
    capability: Literal["equipment_selection"] = "equipment_selection"
    part_id: str = "Part_A"
    process_type: str = "machining"
    constraints: dict[str, Any] = Field(default_factory=dict)


class EquipmentCandidate(CauContract):
    equipment_id: str
    equipment_type: str
    reason: str = ""


class EquipmentSelectionArtifact(CauContract):
    schema_version: str = "1.0"
    artifact_type: Literal["equipment_selection"] = "equipment_selection"
    request_id: str
    global_task_id: str
    selection_id: str
    selected_equipment: list[EquipmentCandidate]
    deterministic: bool = True


class ProcessInformationRequest(CauRequest):
    capability: Literal["process_information"] = "process_information"
    topic: str = "capabilities"


class ProcessInformationArtifact(CauContract):
    schema_version: str = "1.0"
    artifact_type: Literal["process_information"] = "process_information"
    request_id: str
    global_task_id: str
    information_id: str
    supported_capabilities: list[str]
    supported_processes: list[str]
    supported_materials: list[str]
    notes: list[str] = Field(default_factory=list)


class ErrorArtifact(CauContract):
    schema_version: str = "1.0"
    artifact_type: Literal["error"] = "error"
    request_id: str | None = None
    global_task_id: str | None = None
    capability: str | None = None
    error_code: str
    message: str
    details: list[str] = Field(default_factory=list)
