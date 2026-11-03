from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_shared.config import load_config


BASE_YAML = """
environment: development
supervisor:
  name: super-agent
  host: 127.0.0.1
  port: 9000
agents:
  skku_robot:
    name: skku-agent
    enabled: true
    mode: remote
    a2a:
      host: 127.0.0.1
      port: 9001
      base_url: http://127.0.0.1:9001
    backend:
      type: mcp
    mcp:
      transport: streamable-http
      url: http://127.0.0.1:8000/mcp
  cau_process:
    name: cau-process-agent
    enabled: true
    mode: dummy
    a2a:
      host: 127.0.0.1
      port: 9002
      base_url: http://127.0.0.1:9002
    backend:
      type: dummy
"""


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.yaml_file = self.root / "agents.yaml"
        self.yaml_file.write_text(BASE_YAML, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_loads_yaml(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config(self.yaml_file, self.root / "missing.env")

        self.assertEqual(config.supervisor.port, 9000)
        self.assertEqual(config.super_agent.port, 9000)
        self.assertEqual(str(config.agents["skku_robot"].mcp.url), "http://127.0.0.1:8000/mcp")
        self.assertEqual(config.agents["cau_process"].a2a.port, 9002)

    def test_dotenv_overrides_yaml(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text(
            "SUPER_AGENT_SUPERVISOR__PORT=9100\n"
            "TWINVERSE_MCP_URL=http://127.0.0.1:8100/mcp\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {}, clear=True):
            config = load_config(self.yaml_file, env_file)

        self.assertEqual(config.supervisor.port, 9100)
        self.assertEqual(str(config.agents["skku_robot"].mcp.url), "http://127.0.0.1:8100/mcp")

    def test_twinverse_compatible_supervisor_agent_env_aliases(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text(
            'SUPERVISOR_AGENT_MODEL="openrouter:gemini-3-flash-preview"\n'
            "SUPERVISOR_AGENT_KWARGS='{\"reasoning_effort\":\"minimal\"}'\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {}, clear=True):
            config = load_config(self.yaml_file, env_file)

        self.assertEqual(config.llm.supervisor_agent_model, "openrouter:gemini-3-flash-preview")
        self.assertEqual(config.llm.supervisor_agent_kwargs, {"reasoning_effort": "minimal"})

    def test_os_environment_wins_over_dotenv(self) -> None:
        env_file = self.root / ".env"
        env_file.write_text("SUPER_AGENT_AGENTS__SKKU_ROBOT__ENABLED=false\n", encoding="utf-8")

        with patch.dict(os.environ, {"SUPER_AGENT_AGENTS__SKKU_ROBOT__ENABLED": "true"}, clear=True):
            config = load_config(self.yaml_file, env_file)

        self.assertTrue(config.agents["skku_robot"].enabled)

if __name__ == "__main__":
    unittest.main()
