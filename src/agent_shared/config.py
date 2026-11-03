"""YAML and environment based configuration for Super Agent.

Precedence, from highest to lowest:
1. Operating-system environment variables
2. Values loaded from the selected .env file
3. YAML configuration
4. Pydantic model defaults
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml
from dotenv import dotenv_values
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, PositiveFloat, model_validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config" / "agents.yaml"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class A2AEndpointConfig(BaseModel):
    """Connection information for an A2A agent endpoint."""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=9000, ge=1, le=65535)
    base_url: AnyHttpUrl | None = None
    path_prefix: str = ""
    timeout_seconds: PositiveFloat = 60.0
    retries: int = Field(default=2, ge=0)
    streaming: bool = False

    @model_validator(mode="after")
    def normalize_path_prefix(self) -> "A2AEndpointConfig":
        if self.path_prefix and not self.path_prefix.startswith("/"):
            self.path_prefix = f"/{self.path_prefix}"
        if self.path_prefix == "/":
            self.path_prefix = ""
        return self

    def base_url_string(self) -> str:
        if self.base_url is not None:
            return str(self.base_url).rstrip("/")
        return f"http://{self.host}:{self.port}"

    def interface_url(self) -> str:
        return f"{self.base_url_string()}{self.path_prefix}"


class BackendConfig(BaseModel):
    """Backend adapter selection for an agent."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["dummy", "mcp", "rest", "mqtt", "a2a"] = "dummy"


class DummyBackendConfig(BaseModel):
    """Fixture-backed dummy backend settings."""

    model_config = ConfigDict(extra="forbid")

    fixture_file: Path = Path("config/fixtures/cau_process_plan.yaml")

    def resolved_fixture_file(self, root: Path = PROJECT_ROOT) -> Path:
        return self.fixture_file if self.fixture_file.is_absolute() else root / self.fixture_file


class McpEndpointConfig(BaseModel):
    """Connection information for an MCP server used by an agent."""

    model_config = ConfigDict(extra="forbid")

    transport: Literal["streamable-http", "sse", "stdio", "kafka"] = "streamable-http"
    url: AnyHttpUrl | None = None
    timeout_seconds: PositiveFloat = 60.0

    @model_validator(mode="after")
    def require_url_for_network_transport(self) -> "McpEndpointConfig":
        if self.transport in {"streamable-http", "sse"} and self.url is None:
            raise ValueError(f"MCP URL is required for {self.transport} transport")
        return self


class AgentConfig(BaseModel):
    """A2A agent and its optional downstream MCP endpoint."""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    mode: Literal["dummy", "remote"] = "remote"
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    a2a: A2AEndpointConfig
    backend: BackendConfig = Field(default_factory=BackendConfig)
    dummy: DummyBackendConfig = Field(default_factory=DummyBackendConfig)
    mcp: McpEndpointConfig | None = None


class SupervisorConfig(BaseModel):
    """Local server settings for the orchestrating agent."""

    model_config = ConfigDict(extra="forbid")

    name: str = "super-agent"
    host: str = "127.0.0.1"
    port: int = Field(default=9000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    request_timeout_seconds: PositiveFloat = 60.0


class RegistryConfig(BaseModel):
    """Capability registry behavior."""

    model_config = ConfigDict(extra="forbid")

    allow_first_candidate_for_duplicates: bool = False


class PilotConfig(BaseModel):
    """Pilot workflow defaults that should stay configurable."""

    model_config = ConfigDict(extra="forbid")

    default_source: str = "InputStation"


class LlmConfig(BaseModel):
    """Twinverse-compatible supervisor LLM settings.

    The env variable names intentionally match Twinverse MCP Client:
    SUPERVISOR_AGENT_MODEL and SUPERVISOR_AGENT_KWARGS.
    """

    model_config = ConfigDict(extra="forbid")

    supervisor_agent_model: str = "openrouter:gemini-3-flash-preview"
    supervisor_agent_kwargs: dict[str, Any] = Field(default_factory=lambda: {"reasoning_effort": "minimal"})


class KafkaEventsConfig(BaseModel):
    """Optional broker-backed copy of redacted workflow events."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    bootstrap_servers: str = "127.0.0.1:19092"
    topic: str = Field(default="a2a.workflow.events", min_length=1, max_length=249, pattern=r"^[a-zA-Z0-9._-]+$")
    queue_capacity: int = Field(default=1000, ge=1, le=10000)
    max_record_bytes: int = Field(default=262144, ge=1024, le=900000)


class AppConfig(BaseModel):
    """Validated application configuration."""

    model_config = ConfigDict(extra="forbid")

    environment: Literal["development", "test", "production"] = "development"
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    pilot: PilotConfig = Field(default_factory=PilotConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    kafka_events: KafkaEventsConfig = Field(default_factory=KafkaEventsConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_super_agent_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and "super_agent" in data:
            data = dict(data)
            legacy = data.pop("super_agent")
            if "supervisor" not in data:
                data["supervisor"] = legacy
            elif isinstance(data["supervisor"], dict) and isinstance(legacy, dict):
                data["supervisor"] = {**data["supervisor"], **legacy}
        return data

    @property
    def super_agent(self) -> SupervisorConfig:
        """Backward-compatible alias for older tests/scripts."""

        return self.supervisor

    def enabled_agents(self) -> dict[str, AgentConfig]:
        return {key: agent for key, agent in self.agents.items() if agent.enabled}

    def require_agent(self, agent_id: str) -> AgentConfig:
        try:
            return self.agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"Agent is not configured: {agent_id}") from exc


ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "SKKU_AGENT_URL": ("agents", "skku_robot", "a2a", "base_url"),
    "SKKU_AGENT_HOST": ("agents", "skku_robot", "a2a", "host"),
    "SKKU_AGENT_PORT": ("agents", "skku_robot", "a2a", "port"),
    "TWINVERSE_MCP_URL": ("agents", "skku_robot", "mcp", "url"),
    "TWINVERSE_MCP_TIMEOUT_SECONDS": ("agents", "skku_robot", "mcp", "timeout_seconds"),
    "CAU_AGENT_URL": ("agents", "cau_process", "a2a", "base_url"),
    "CAU_AGENT_HOST": ("agents", "cau_process", "a2a", "host"),
    "CAU_AGENT_PORT": ("agents", "cau_process", "a2a", "port"),
    "SUPERVISOR_AGENT_MODEL": ("llm", "supervisor_agent_model"),
    "SUPERVISOR_AGENT_KWARGS": ("llm", "supervisor_agent_kwargs"),
}

ENV_PREFIXES = ("SUPER_AGENT_",)
RESERVED_ENV_KEYS = {
    "SUPER_AGENT_CONFIG_FILE",
    "SUPER_AGENT_ENV_FILE",
}


def _parse_env_value(value: str) -> Any:
    """Use YAML scalar parsing so booleans and numbers keep their types."""

    parsed = yaml.safe_load(value)
    return value if parsed is None and value.strip().lower() != "null" else parsed


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for part in path[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[path[-1]] = value


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    return data


def _combined_environment(env_file: Path | None) -> dict[str, str]:
    # .env를 읽은 뒤 실제 프로세스 환경 변수를 덮어 적용합니다.
    values: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        values.update({key: value for key, value in dotenv_values(env_file).items() if value is not None})
    values.update(os.environ)
    return values


def _apply_environment_overrides(data: dict[str, Any], environment: Mapping[str, str]) -> None:
    # CAU_AGENT_URL 같은 짧은 별칭을 먼저 적용합니다.
    # 같은 값을 범용 중첩 변수로도 지정했다면 그 값이 마지막에 적용됩니다.
    for key, path in ENV_ALIASES.items():
        if key in environment:
            _set_nested(data, path, _parse_env_value(environment[key]))

    for key, value in environment.items():
        if key in RESERVED_ENV_KEYS:
            continue
        for prefix in ENV_PREFIXES:
            if key.startswith(prefix):
                suffix = key.removeprefix(prefix)
                path = tuple(part.lower() for part in suffix.split("__") if part)
                if path:
                    _set_nested(data, path, _parse_env_value(value))
                break


def load_config(
    config_file: str | Path | None = None,
    env_file: str | Path | None = None,
) -> AppConfig:
    """YAML을 읽고 .env·환경 변수로 덮은 뒤 전체 설정을 검증합니다."""

    env_selector = os.getenv("SUPER_AGENT_ENV_FILE")
    initial_env_path = Path(env_file) if env_file is not None else Path(env_selector or DEFAULT_ENV_FILE)
    environment = _combined_environment(initial_env_path)

    selected_config = (
        config_file
        or environment.get("SUPER_AGENT_CONFIG_FILE")
        or DEFAULT_CONFIG_FILE
    )
    config_path = Path(selected_config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    data = _read_yaml(config_path)
    _apply_environment_overrides(data, environment)
    return AppConfig.model_validate(data)


def main() -> None:
    """Print the effective validated configuration for diagnostics."""

    config = load_config()
    print(json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
