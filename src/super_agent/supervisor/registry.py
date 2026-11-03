"""설정에 등록된 기능을 해당 기관의 A2A 주소로 연결합니다."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from agent_shared.config import AgentConfig, AppConfig


class AgentEndpoint(BaseModel):
    """A resolved A2A endpoint for a capable agent."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    name: str
    base_url: str
    timeout_seconds: float = Field(gt=0)
    streaming: bool = False


class AgentRegistration(BaseModel):
    """Registry entry loaded from YAML-backed agent configuration."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    name: str
    enabled: bool = True
    capabilities: list[str] = Field(default_factory=list)
    endpoint: AgentEndpoint

    @classmethod
    def from_agent_config(cls, agent_id: str, agent: AgentConfig) -> "AgentRegistration":
        return cls(
            agent_id=agent_id,
            name=agent.name,
            enabled=agent.enabled,
            capabilities=list(agent.capabilities),
            endpoint=AgentEndpoint(
                agent_id=agent_id,
                name=agent.name,
                base_url=agent.a2a.interface_url(),
                timeout_seconds=float(agent.a2a.timeout_seconds),
                streaming=agent.a2a.streaming,
            ),
        )


class CapabilityRegistry:
    """Resolve a capability to exactly one enabled A2A agent endpoint."""

    def __init__(
        self,
        registrations: Iterable[AgentRegistration],
        *,
        allow_first_candidate_for_duplicates: bool = False,
    ) -> None:
        self._registrations = {
            registration.agent_id: registration
            for registration in sorted(registrations, key=lambda item: item.agent_id)
        }
        self.allow_first_candidate_for_duplicates = allow_first_candidate_for_duplicates

    @classmethod
    def from_config(cls, config: AppConfig) -> "CapabilityRegistry":
        return cls(
            (
                AgentRegistration.from_agent_config(agent_id, agent)
                for agent_id, agent in config.agents.items()
            ),
            allow_first_candidate_for_duplicates=config.registry.allow_first_candidate_for_duplicates,
        )

    async def resolve(self, capability: str, agent_id: str | None = None) -> AgentEndpoint:
        # 같은 기능을 여러 기관이 제공하면 명시적 선택이나 설정된 우선순위가 필요합니다.
        candidates = [
            registration
            for registration in self._registrations.values()
            if registration.enabled and capability in registration.capabilities and (agent_id is None or registration.agent_id == agent_id)
        ]
        if not candidates:
            raise UnsupportedCapabilityError(f"No enabled agent supports capability: {capability}" + (f" (requested agent: {agent_id})" if agent_id else ""))
        if len(candidates) > 1 and not self.allow_first_candidate_for_duplicates:
            agent_ids = ", ".join(candidate.agent_id for candidate in candidates)
            raise AmbiguousCapabilityError(
                f"Multiple enabled agents support capability {capability}: {agent_ids}"
            )
        return candidates[0].endpoint

    def registrations(self) -> list[AgentRegistration]:
        return list(self._registrations.values())

    def capability_index(self) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for registration in self._registrations.values():
            if not registration.enabled:
                continue
            for capability in registration.capabilities:
                index.setdefault(capability, []).append(registration.agent_id)
        return index


class UnsupportedCapabilityError(LookupError):
    """Raised when no enabled agent supports a requested capability."""


class AmbiguousCapabilityError(LookupError):
    """Raised when multiple candidates exist without explicit first-candidate policy."""
