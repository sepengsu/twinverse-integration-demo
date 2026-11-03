"""Backend protocol and exceptions for CAU process-agent adapters."""

from __future__ import annotations

from typing import Protocol

from cau.contracts import (
    EquipmentSelectionArtifact,
    EquipmentSelectionRequest,
    ProcessInformationArtifact,
    ProcessInformationRequest,
    ProcessPlanArtifact,
    ProcessPlanningRequest,
)


class CauBackendError(RuntimeError):
    """Raised when a CAU backend cannot complete a valid request."""


class CauBackend(Protocol):
    async def create_process_plan(self, request: ProcessPlanningRequest) -> ProcessPlanArtifact:
        """Create a process plan artifact."""

    async def select_equipment(self, request: EquipmentSelectionRequest) -> EquipmentSelectionArtifact:
        """Select equipment for a requested process."""

    async def get_process_information(self, request: ProcessInformationRequest) -> ProcessInformationArtifact:
        """Return process-capability information."""
