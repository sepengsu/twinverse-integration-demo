"""한 PC에서 CAU·SKKU·Supervisor를 함께 띄우는 시연용 실행기입니다.

선택하면 형제 폴더의 Twinverse FastMCP도 시작합니다. 원격 CAU PC와 연결할 때는
로컬 CAU까지 실행하는 이 명령 대신 각 서버를 따로 실행합니다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib import error, request

from agent_shared.config import PROJECT_ROOT, load_config


class ManagedProcess:
    """A subprocess owned by the local stack runner."""

    def __init__(self, name: str, process: subprocess.Popen[bytes]) -> None:
        self.name = name
        self.process = process


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Super Agent Phase 3 stack.")
    parser.add_argument("--config", default=None, help="Path to agents YAML file.")
    parser.add_argument("--env-file", default=None, help="Path to .env file.")
    parser.add_argument(
        "--start-twinverse",
        action="store_true",
        help="Also start the sibling Twinverse FastMCP server before SKKU.",
    )
    parser.add_argument(
        "--twinverse-dir",
        default=str(PROJECT_ROOT.parent / "twinverse"),
        help="Path to the sibling twinverse repository.",
    )
    parser.add_argument(
        "--twinverse-startup-seconds",
        type=float,
        default=4.0,
        help="Seconds to give Twinverse FastMCP before starting A2A services.",
    )
    parser.add_argument(
        "--health-timeout-seconds",
        type=float,
        default=30.0,
        help="Timeout for each A2A service health check.",
    )
    parser.add_argument(
        "--run-sample",
        action="store_true",
        help="Submit a sample workflow after the Supervisor becomes healthy.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config, args.env_file)
    env = _child_environment(args.config, args.env_file)
    processes: list[ManagedProcess] = []

    try:
        # 의존 서버를 먼저 시작하고 상태 확인을 마친 다음 Supervisor를 엽니다.
        if args.start_twinverse:
            processes.append(_start_twinverse(Path(args.twinverse_dir), env))
            print(f"[stack] Waiting {args.twinverse_startup_seconds:.1f}s for Twinverse FastMCP startup...", flush=True)
            time.sleep(args.twinverse_startup_seconds)

        cau = config.require_agent("cau_process")
        skku = config.require_agent("skku_robot")

        processes.append(
            _start_python_module(
                "cau",
                "cau.a2a_server",
                env,
                config_file=args.config,
                env_file=args.env_file,
            )
        )
        _wait_http_ok(f"{cau.a2a.base_url_string()}/health", args.health_timeout_seconds)

        processes.append(
            _start_python_module(
                "skku",
                "skku.app",
                env,
                config_file=args.config,
                env_file=args.env_file,
            )
        )
        _wait_http_ok(f"{skku.a2a.base_url_string()}/health", args.health_timeout_seconds)

        processes.append(
            _start_python_module(
                "supervisor",
                "super_agent.supervisor.app",
                env,
                config_file=args.config,
                env_file=args.env_file,
            )
        )
        supervisor_url = f"http://{config.supervisor.host}:{config.supervisor.port}"
        _wait_http_ok(f"{supervisor_url}/health", args.health_timeout_seconds)

        print("", flush=True)
        print("[stack] Local Phase 3 stack is running.", flush=True)
        print(f"[stack] Supervisor: {supervisor_url}", flush=True)
        print(f"[stack] Dashboard:  {supervisor_url}/dashboard", flush=True)
        print(f"[stack] CAU:        {cau.a2a.base_url_string()}", flush=True)
        print(f"[stack] SKKU:       {skku.a2a.base_url_string()}", flush=True)
        print("[stack] Press Ctrl+C to stop all child processes.", flush=True)

        if args.run_sample:
            response = _post_json(
                f"{supervisor_url}/workflow",
                {
                    "global_task_id": f"TASK-{int(time.time())}",
                    "goal": "Plan a robot path directly from the workpiece to the work table.",
                    "part_id": "skku/workpiece_01",
                    "source": "skku/workpiece_01",
                    "destination": "skku/work_table_01",
                },
            )
            print("[stack] Sample workflow response:", flush=True)
            print(json.dumps(response, ensure_ascii=False, indent=2), flush=True)

        while True:
            for managed in processes:
                return_code = managed.process.poll()
                if return_code is not None:
                    raise RuntimeError(f"{managed.name} exited unexpectedly with code {return_code}.")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[stack] Stopping local stack...", flush=True)
        return 0
    finally:
        _stop_processes(processes)


def _child_environment(config_file: str | None, env_file: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("UV_CACHE_DIR", str(PROJECT_ROOT / ".uv-cache"))
    if config_file:
        env["SUPER_AGENT_CONFIG_FILE"] = config_file
    if env_file:
        env["SUPER_AGENT_ENV_FILE"] = env_file
    return env


def _start_python_module(
    name: str,
    module: str,
    env: dict[str, str],
    *,
    config_file: str | None,
    env_file: str | None,
) -> ManagedProcess:
    command = [sys.executable, "-m", module]
    if config_file:
        command.extend(["--config", config_file])
    if env_file:
        command.extend(["--env-file", env_file])
    print(f"[stack] Starting {name}: {' '.join(command)}", flush=True)
    return ManagedProcess(
        name,
        subprocess.Popen(command, cwd=str(PROJECT_ROOT), env=env),
    )


def _start_twinverse(twinverse_dir: Path, env: dict[str, str]) -> ManagedProcess:
    if not twinverse_dir.is_dir():
        raise FileNotFoundError(f"Twinverse directory not found: {twinverse_dir}")
    uv = shutil.which("uv")
    if uv is None:
        raise FileNotFoundError("uv executable was not found on PATH.")
    command = [uv, "run", "--package", "twinverse-mcp-server", "twinverse-mcp-server", "http"]
    print(f"[stack] Starting twinverse-mcp: {' '.join(command)}", flush=True)
    return ManagedProcess(
        "twinverse-mcp",
        subprocess.Popen(command, cwd=str(twinverse_dir), env=env),
    )


def _wait_http_ok(url: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with request.urlopen(url, timeout=1.0) as response:
                if 200 <= response.status < 300:
                    print(f"[stack] Healthy: {url}", flush=True)
                    return
        except Exception as exc:
            last_error = exc
            time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {url}: {last_error}")


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    http_request = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=120.0) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body}") from exc
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected JSON object from {url}.")
    return parsed


def _stop_processes(processes: list[ManagedProcess]) -> None:
    for managed in reversed(processes):
        if managed.process.poll() is None:
            print(f"[stack] Terminating {managed.name}...", flush=True)
            _terminate(managed.process)
    for managed in reversed(processes):
        try:
            managed.process.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            print(f"[stack] Killing {managed.name}...", flush=True)
            managed.process.kill()
            managed.process.wait(timeout=8.0)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        process.terminate()
    else:
        process.send_signal(signal.SIGTERM)


if __name__ == "__main__":
    raise SystemExit(main())
