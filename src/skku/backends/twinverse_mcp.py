"""Twinverse FastMCP의 경로 도구만 사용하는 SKKU 계획 전용 백엔드입니다."""

from __future__ import annotations

import json
import hashlib
from collections.abc import Sequence
from typing import Any

from fastmcp import Client
from fastmcp.client.client import CallToolResult
from pydantic import BaseModel

from agent_shared.config import McpEndpointConfig
from skku.backend import (
    McpToolUnavailableError,
    McpUnavailableError,
    PathGenerationError,
    PathValidationError,
    SafetyViolationError,
    UnknownRobotObjectError,
)
from skku.models import (
    McpToolResult,
    PathValidationArtifact,
    RobotPath,
    RobotPlanArtifact,
    RobotStateArtifact,
    RobotTarget,
    RobotTaskRequest,
    RobotValidation,
)
from skku.prompts import BLOCKED_EXECUTION_ACTIONS, SAFE_PLAN_ONLY_MANAGE_PATH_ACTIONS


class TwinverseMcpBackend:
    """경로를 발행하며 실물 로봇의 이동 명령은 호출하지 않습니다."""

    def __init__(
        self,
        transport: Any,
        *,
        timeout_seconds: float = 30.0,
        required_tools: Sequence[str] | None = None,
    ) -> None:
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.required_tools = tuple(required_tools or ("manage_path",))

    @classmethod
    def from_config(cls, mcp: McpEndpointConfig) -> "TwinverseMcpBackend":
        if mcp.url is None:
            raise ValueError("SKKU Twinverse MCP URL is required.")
        return cls(str(mcp.url), timeout_seconds=float(mcp.timeout_seconds))

    async def plan_robot_task(self, request: RobotTaskRequest) -> RobotPlanArtifact:
        # 출발·목적 개체를 확인하고 상승/접근을 포함한 네 타깃을 구성합니다.
        self._require_plan_only(request)
        source = self._require_endpoint(request, "source")
        destination = self._require_endpoint(request, "destination")
        self._validate_known_object(request, source, "source")
        self._validate_known_object(request, destination, "destination")

        path_id = self._path_id(request)
        destination_object_id = self._destination_object_id(request, destination)
        offset = self._offset(request)
        clearance = max(0.2, offset[1] + 0.3)
        targets = [
            RobotTarget(target_id=f"{path_id}-source", object_id=source, role="source_anchor", anchor="center_top", offset=offset),
            RobotTarget(target_id=f"{path_id}-lift", object_id=source, role="lift", anchor="center_top", offset=[offset[0], clearance, offset[2]]),
            RobotTarget(target_id=f"{path_id}-approach", object_id=destination_object_id, role="approach", anchor="center_top", offset=[offset[0], clearance, offset[2]]),
            RobotTarget(target_id=f"{path_id}-destination", object_id=destination_object_id, role="destination_anchor", anchor="center_top", offset=offset),
        ]

        try:
            # make_path, 타깃 생성, 연결까지만 허용합니다. 발행 성공과 이동 완료는 다릅니다.
            async with Client(self.transport, timeout=self.timeout_seconds) as client:
                await self._require_tools(client, self.required_tools)
                results = [await self._safe_call_manage_path(client, "make_path", {"robot": "GP25", "name": path_id})]
                for target in targets:
                    results.append(await self._safe_call_manage_path(client, "make_object_anchor_target", {
                        "robot": "GP25", "path": path_id, "reference_object_id": target.object_id,
                        "name": target.target_id, "anchor": target.anchor, "offset": target.offset,
                    }))
                results.append(await self._safe_call_manage_path(client, "connect_targets", {"robot": "GP25", "path": path_id}))
        except (McpUnavailableError, McpToolUnavailableError, PathGenerationError, SafetyViolationError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise McpUnavailableError(f"Twinverse FastMCP server is unavailable: {exc}") from exc

        validation = self._validate_tool_results(request, results)
        plan = RobotPlanArtifact(
            global_task_id=request.global_task_id,
            request_id=request.request_id,
            plan_id=self._plan_id(request),
            status="planned" if validation.publication_verified else "failed",
            source=source,
            destination=destination,
            targets=targets,
            path=RobotPath(
                path_id=path_id,
                transport="mcp",
                tool_actions=[result.action or result.tool for result in results],
            ),
            validation=validation,
            mcp_results=results,
        )
        if plan.validation.errors:
            raise PathValidationError("Twinverse path validation failed.")
        return plan

    async def get_robot_state(self, request: RobotTaskRequest) -> RobotStateArtifact:
        try:
            async with Client(self.transport, timeout=self.timeout_seconds) as client:
                tools = {tool.name for tool in await self._list_tools(client)}
                if "see_virtual_objects" not in tools:
                    raise McpToolUnavailableError("Required MCP tool is unavailable: see_virtual_objects")
                virtual = await self._call_tool(client, "see_virtual_objects", {})
                console: dict[str, Any] = {}
                if "read_console" in tools:
                    console = self._coerce_result_data(
                        await self._call_tool(
                            client,
                            "read_console",
                            {
                                "action": "get",
                                "types": ["error", "warning"],
                                "count": 20,
                                "format": "json",
                                "include_stacktrace": False,
                            },
                        )
                    )
        except (McpUnavailableError, McpToolUnavailableError, SafetyViolationError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise McpUnavailableError(f"Twinverse FastMCP server is unavailable: {exc}") from exc

        raw_virtual = self._jsonable(virtual.data)
        objects = self._summarize_objects(raw_virtual)
        console_errors = self._extract_console_errors(console)
        return RobotStateArtifact(
            global_task_id=request.global_task_id,
            request_id=request.request_id,
            state_id=f"RS-{request.global_task_id}",
            status="degraded" if console_errors else "available",
            objects=objects,
            console_errors=console_errors,
            raw={
                "virtual_objects": raw_virtual,
                "console": console,
            },
        )

    async def validate_robot_plan(
        self,
        request: RobotTaskRequest,
        plan: RobotPlanArtifact | None = None,
    ) -> PathValidationArtifact:
        # 이 구현에는 기하학적 충돌 검사기가 없으므로 미검사를 통과로 표시하지 않습니다.
        if plan is None:
            validation = RobotValidation(
                collision_free=None,
                warnings=["No geometric collision checker was run."],
                checks=[
                    "request_schema_valid",
                    "execution_mode_plan_only",
                    "no_physical_execution_tool_selected",
                ],
            )
        else:
            validation = plan.validation
        status = "not_checked" if validation.collision_free is None else "validated" if validation.collision_free else "invalid"
        return PathValidationArtifact(
            global_task_id=request.global_task_id,
            request_id=request.request_id,
            validation_id=f"RV-{request.global_task_id}",
            status=status,
            validation=validation,
        )

    async def _safe_call_manage_path(
        self,
        client: Client,
        action: str,
        payload: dict[str, Any],
    ) -> McpToolResult:
        # MCP 서버가 더 많은 기능을 제공해도 계획에 필요한 동작만 통과시킵니다.
        if action in BLOCKED_EXECUTION_ACTIONS or action not in SAFE_PLAN_ONLY_MANAGE_PATH_ACTIONS:
            raise SafetyViolationError(f"Blocked Twinverse robot execution action: {action}")
        result = await self._call_tool(client, "manage_path", {"action": action, "json": payload})
        data = self._coerce_result_data(result)
        success = bool(data.get("success", not result.is_error))
        if not success:
            raise PathGenerationError(str(data.get("message") or f"manage_path failed for {action}"))
        return McpToolResult(
            tool="manage_path",
            action=action,
            success=success,
            message=str(data.get("message", "")),
            data=self._jsonable(data.get("data", {})),
        )

    async def _call_tool(self, client: Client, name: str, arguments: dict[str, Any]) -> CallToolResult:
        # 경로 생성과 무관한 직접 이동·시나리오 실행 도구는 호출 경계에서 차단합니다.
        if name in {
            "manage_scenario",
            "move_tcp_to_object_offset",
            "move_tcp_to_object_offset_by_table",
            "move_tcp_to_object_left_front_corner",
            "execute_menu_item",
            "execute_csharp_code",
        }:
            raise SafetyViolationError(f"Tool is blocked in Phase 2 plan-only mode: {name}")
        try:
            return await client.call_tool(
                name,
                arguments,
                timeout=self.timeout_seconds,
                raise_on_error=False,
            )
        except Exception as exc:  # noqa: BLE001 - convert MCP transport failures at the backend boundary.
            raise McpUnavailableError(f"Twinverse FastMCP tool call failed: {exc}") from exc

    async def _require_tools(self, client: Client, names: Sequence[str]) -> None:
        tools = {tool.name for tool in await self._list_tools(client)}
        missing = [name for name in names if name not in tools]
        if missing:
            raise McpToolUnavailableError(f"Required MCP tools are unavailable: {', '.join(missing)}")

    async def _list_tools(self, client: Client):
        try:
            return await client.list_tools()
        except Exception as exc:  # noqa: BLE001
            raise McpUnavailableError(f"Twinverse FastMCP server is unavailable: {exc}") from exc

    def _validate_tool_results(
        self,
        request: RobotTaskRequest,
        results: list[McpToolResult],
    ) -> RobotValidation:
        errors = [result.message or f"{result.tool}:{result.action} failed" for result in results if not result.success]
        checks = [
            "request_schema_valid",
            "execution_mode_plan_only",
            "manage_path_make_path_published",
            "manage_path_make_object_anchor_target_published",
            "no_start_path_or_physical_motion",
        ]
        warnings = [
            "Plan publication does not prove host acceptance, reachability, collision freedom or completed motion.",
            "No geometric collision checker was run. collision_free is unknown.",
        ]
        return RobotValidation(
            collision_free=None,
            publication_verified=not errors,
            checks=checks,
            warnings=warnings,
            errors=errors,
        )

    @staticmethod
    def _require_plan_only(request: RobotTaskRequest) -> None:
        if request.execution_mode != "plan_only":
            raise SafetyViolationError("Phase 2 only supports execution_mode='plan_only'.")

    @staticmethod
    def _require_endpoint(request: RobotTaskRequest, field: str) -> str:
        value = getattr(request.input, field)
        if not isinstance(value, str) or not value:
            raise UnknownRobotObjectError(f"Robot planning request requires input.{field}.")
        return value

    @staticmethod
    def _validate_known_object(request: RobotTaskRequest, value: str, field: str) -> None:
        known = request.input.constraints.get("known_objects")
        if not isinstance(known, list):
            return
        known_values = {item for item in known if isinstance(item, str)}
        if value not in known_values:
            raise UnknownRobotObjectError(f"Unknown {field} object: {value}")

    @staticmethod
    def _destination_object_id(request: RobotTaskRequest, fallback: str) -> str:
        constraints = request.input.constraints
        for key in ("destination_object_id", "target_object_id", "reference_object_id"):
            value = constraints.get(key)
            if isinstance(value, str) and value:
                return value
        if request.input.target_equipment_id:
            return request.input.target_equipment_id
        return fallback

    @staticmethod
    def _offset(request: RobotTaskRequest) -> list[float]:
        value = request.input.constraints.get("offset")
        if value is None:
            return [0.0, 0.05, 0.0]
        if not isinstance(value, list) or len(value) != 3:
            raise UnknownRobotObjectError("constraints.offset must be a three-element list.")
        return [float(component) for component in value]

    @staticmethod
    def _plan_id(request: RobotTaskRequest) -> str:
        value = request.input.constraints.get("plan_id")
        return str(value) if value else f"RP-{request.global_task_id}"

    @staticmethod
    def _path_id(request: RobotTaskRequest) -> str:
        value = request.input.constraints.get("path_id")
        return str(value) if value else "Path-" + hashlib.sha256(request.global_task_id.encode()).hexdigest()[:12]

    @staticmethod
    def _target_id(request: RobotTaskRequest) -> str:
        value = request.input.constraints.get("target_id")
        return str(value) if value else "Target_001"

    @staticmethod
    def _coerce_result_data(result: CallToolResult) -> dict[str, Any]:
        data = result.data if result.data is not None else result.structured_content
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="json")
        if isinstance(data, dict):
            return TwinverseMcpBackend._jsonable(data)
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                return {"success": not result.is_error, "message": data}
            if isinstance(parsed, dict):
                return TwinverseMcpBackend._jsonable(parsed)
        return {"success": not result.is_error, "data": TwinverseMcpBackend._jsonable(data)}

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return {str(key): TwinverseMcpBackend._jsonable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [TwinverseMcpBackend._jsonable(item) for item in value]
        if isinstance(value, tuple):
            return [TwinverseMcpBackend._jsonable(item) for item in value]
        if isinstance(value, str | int | float | bool) or value is None:
            return value
        return repr(value)

    @staticmethod
    def _summarize_objects(raw_virtual: Any) -> list[str]:
        if isinstance(raw_virtual, list):
            objects: list[str] = []
            for item in raw_virtual:
                if isinstance(item, str):
                    objects.append(item)
                elif isinstance(item, dict):
                    name = item.get("name") or item.get("id") or item.get("object_id")
                    if isinstance(name, str):
                        objects.append(name)
            return objects
        if isinstance(raw_virtual, dict):
            for key in ("objects", "data", "items"):
                value = raw_virtual.get(key)
                if isinstance(value, list):
                    return TwinverseMcpBackend._summarize_objects(value)
        return []

    @staticmethod
    def _extract_console_errors(console: dict[str, Any]) -> list[str]:
        data = console.get("data", console.get("messages", []))
        if not isinstance(data, list):
            return []
        errors: list[str] = []
        for item in data:
            if isinstance(item, str):
                if "error" in item.lower():
                    errors.append(item)
            elif isinstance(item, dict):
                message_type = str(item.get("type", item.get("logType", ""))).lower()
                message = item.get("message") or item.get("text")
                if "error" in message_type and isinstance(message, str):
                    errors.append(message)
        return errors
