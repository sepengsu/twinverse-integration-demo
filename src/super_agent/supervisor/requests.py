"""Supervisor 요청을 기관별 A2A 입력으로 만들고 CAU 결과를 SKKU 입력으로 변환합니다."""

from __future__ import annotations

from typing import Any

from cau.contracts import (
    EquipmentSelectionRequest,
    ProcessInformationRequest,
    ProcessPlanArtifact,
    ProcessPlanningRequest,
)
from skku.models import RobotTaskInput, RobotTaskRequest
from super_agent.supervisor.workflow import WorkflowRequest


def build_direct_request(request: WorkflowRequest, *, default_source: str) -> dict:
    """선택한 기능의 입력 형식에 맞추되, 작업 연결 ID는 Supervisor가 지정합니다."""
    if not isinstance(request.input.get("constraints", {}), dict):
        raise ValueError("기능별 입력의 constraints는 JSON 객체여야 합니다.")
    correlation = {
        "schema_version": request.schema_version,
        "request_id": request.request_id or f"REQ-DIRECT-{request.global_task_id}",
        "global_task_id": request.global_task_id,
        "capability": request.capability,
    }
    if request.capability in {"robot_path_planning", "robot_state_query", "path_validation"}:
        robot_input = {
            "goal": request.goal, "part_id": request.part_id or None,
            "source": request.source or default_source,
            "destination": request.destination or request.requested_machine_id,
            **request.input,
        }
        constraints = {**request.constraints, **request.input.get("constraints", {})}
        if request.capability == "robot_path_planning" and "known_objects" not in constraints:
            constraints["known_objects"] = [robot_input[key] for key in ("source", "destination") if robot_input.get(key)]
        robot_input["constraints"] = constraints
        robot_input.setdefault("target_equipment_id", robot_input.get("destination"))
        return RobotTaskRequest(**correlation, input=RobotTaskInput.model_validate(robot_input), execution_mode="plan_only").model_dump(mode="json")

    models = {"process_planning": ProcessPlanningRequest, "equipment_selection": EquipmentSelectionRequest, "process_information": ProcessInformationRequest}
    model = models.get(request.capability)
    if model is not None:
        values = dict(request.input)
        if request.capability != "process_information":
            if request.part_id:
                values.setdefault("part_id", request.part_id)
            values["constraints"] = {**request.constraints, **values.get("constraints", {})}
        if request.capability == "process_planning":
            values.setdefault("requested_machine_id", request.requested_machine_id or request.destination)
        return model.model_validate({**values, **correlation}).model_dump(mode="json")

    # 아직 전용 형식이 없는 기능은 원본 입력을 전달합니다.
    # 요청 ID와 기능 이름은 사용자 입력보다 Supervisor 값이 우선합니다.
    return {**request.input, **correlation}


class ProcessPlanToRobotTaskTransformer:
    """CAU 공정 결과의 설비 ID를 SKKU 경로의 목적지로 바꿉니다."""

    def __init__(self, *, default_source: str) -> None:
        if not default_source:
            raise ValueError("default_source must be configured for the pilot transformer.")
        self.default_source = default_source

    def transform(self, process_plan: ProcessPlanArtifact, workflow_request: WorkflowRequest) -> RobotTaskRequest:
        if not process_plan.part_id:
            raise TransformationInputError("ProcessPlanArtifact.part_id is required.")
        if not process_plan.machine_id:
            raise TransformationInputError("ProcessPlanArtifact.machine_id is required.")

        source = self._source_from_request_or_process_data(process_plan, workflow_request)
        if not source:
            raise TransformationInputError("Robot planning source is required.")

        # 목적지는 사용자가 다시 입력한 값이 아니라 CAU가 확정해 반환한 machine_id입니다.
        destination = process_plan.machine_id
        return RobotTaskRequest(
            request_id=f"REQ-RP-{workflow_request.global_task_id}",
            global_task_id=workflow_request.global_task_id,
            capability="robot_path_planning",
            input=RobotTaskInput(
                goal=f"Move {process_plan.part_id} from {source} to {destination}",
                part_id=process_plan.part_id,
                source=source,
                destination=destination,
                target_equipment_id=destination,
                constraints={
                    "known_objects": [source, destination],
                },
            ),
            execution_mode="plan_only",
        )

    def _source_from_request_or_process_data(
        self,
        process_plan: ProcessPlanArtifact,
        workflow_request: WorkflowRequest,
    ) -> str:
        # 출발지는 사용자 지정 → 공정 단계의 source/station → 기본값 순으로 찾습니다.
        if workflow_request.source:
            return workflow_request.source

        for operation in process_plan.operations:
            source = _string_from_mapping(operation.parameters, "source")
            if source:
                return source
            if operation.name.lower() == "load":
                station = _string_from_mapping(operation.parameters, "station")
                if station:
                    return station

        return self.default_source


class TransformationInputError(ValueError):
    """Raised when upstream artifacts do not contain data required for transformation."""


def _string_from_mapping(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    return None
