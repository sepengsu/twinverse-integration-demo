"""SKKU 로봇 경로 요청과 결과 Artifact의 형식입니다."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RobotCapability = Literal["robot_path_planning", "robot_state_query", "path_validation"]
# 실행 모드는 현재 계획 전용 하나만 허용합니다. 이동 실행을 추가하려면 별도 설계가 필요합니다.
ExecutionMode = Literal["plan_only"]


class RobotContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RobotTaskInput(RobotContract):
    goal: str = ""
    part_id: str | None = None
    source: str | None = None
    destination: str | None = None
    target_equipment_id: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_endpoint_for_planning(self) -> "RobotTaskInput":
        return self


class RobotTaskRequest(RobotContract):
    schema_version: str = "1.0"
    request_id: str | None = None
    global_task_id: str
    capability: RobotCapability
    input: RobotTaskInput = Field(default_factory=RobotTaskInput)
    execution_mode: ExecutionMode = "plan_only"


class RobotTarget(RobotContract):
    target_id: str
    object_id: str | None = None
    role: str = "waypoint"
    anchor: str | None = None
    offset: list[float] | None = None


class RobotPath(RobotContract):
    path_id: str
    transport: Literal["mcp", "mqtt"] = "mcp"
    tool_actions: list[str] = Field(default_factory=list)


class RobotValidation(RobotContract):
    # None은 충돌 여부를 확인하지 않았다는 뜻입니다. False/True와 구분해야 합니다.
    collision_free: bool | None = None
    collision_check: Literal["not_performed", "passed", "failed"] = "not_performed"
    publication_verified: bool = False
    checks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class McpToolResult(RobotContract):
    tool: str
    action: str | None = None
    success: bool
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class RobotPlanArtifact(RobotContract):
    # MCP 경로 발행 결과입니다. Unity 승인이나 실물 로봇의 이동 완료 결과가 아닙니다.
    schema_version: str = "1.0"
    artifact_type: Literal["robot_plan"] = "robot_plan"
    global_task_id: str
    request_id: str | None = None
    plan_id: str
    status: Literal["planned", "validated", "invalid", "failed"] = "planned"
    execution_mode: ExecutionMode = "plan_only"
    source: str
    destination: str
    targets: list[RobotTarget] = Field(default_factory=list)
    path: RobotPath
    validation: RobotValidation
    mcp_results: list[McpToolResult] = Field(default_factory=list)


class RobotStateArtifact(RobotContract):
    schema_version: str = "1.0"
    artifact_type: Literal["robot_state"] = "robot_state"
    global_task_id: str
    request_id: str | None = None
    state_id: str
    status: Literal["available", "degraded", "unavailable"] = "available"
    objects: list[str] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class PathValidationArtifact(RobotContract):
    schema_version: str = "1.0"
    artifact_type: Literal["path_validation"] = "path_validation"
    global_task_id: str
    request_id: str | None = None
    validation_id: str
    status: Literal["validated", "invalid", "not_checked"] = "not_checked"
    validation: RobotValidation


class ErrorArtifact(RobotContract):
    schema_version: str = "1.0"
    artifact_type: Literal["error"] = "error"
    request_id: str | None = None
    global_task_id: str | None = None
    capability: str | None = None
    error_code: str
    message: str
    details: list[str] = Field(default_factory=list)
