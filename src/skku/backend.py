"""Backend protocol and exceptions for SKKU robot adapters."""

from __future__ import annotations

from typing import Protocol

from skku.models import (
    PathValidationArtifact,
    RobotPlanArtifact,
    RobotStateArtifact,
    RobotTaskRequest,
)


class RobotBackendError(RuntimeError):
    """Base error for robot backend failures."""


class McpUnavailableError(RobotBackendError):
    """Raised when the Twinverse FastMCP endpoint cannot be reached."""


class McpToolUnavailableError(RobotBackendError):
    """Raised when a required Twinverse MCP tool is unavailable."""


class UnknownRobotObjectError(RobotBackendError):
    """Raised when source or destination information is missing or unknown."""


class PathGenerationError(RobotBackendError):
    """Raised when the MCP path-generation tool reports failure."""


class PathValidationError(RobotBackendError):
    """Raised when a generated path fails validation."""


class SafetyViolationError(RobotBackendError):
    """Raised when a backend is asked to execute a physical action."""


class RobotBackend(Protocol):
    async def plan_robot_task(self, request: RobotTaskRequest) -> RobotPlanArtifact:
        """Plan a robot task without executing physical motion."""

    async def get_robot_state(self, request: RobotTaskRequest) -> RobotStateArtifact:
        """Query robot/Twinverse state."""

    async def validate_robot_plan(
        self,
        request: RobotTaskRequest,
        plan: RobotPlanArtifact | None = None,
    ) -> PathValidationArtifact:
        """Validate a robot plan."""
