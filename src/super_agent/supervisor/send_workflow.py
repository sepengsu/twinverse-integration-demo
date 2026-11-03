"""Submit a workflow request to a running Phase 3 Supervisor."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from typing import Any
from urllib import error, request


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a manufacturing workflow to the Supervisor.")
    parser.add_argument("--url", default="http://127.0.0.1:9000/workflow", help="Supervisor workflow endpoint.")
    parser.add_argument("--global-task-id", default=None, help="Workflow ID. Defaults to TASK-<unix-time>.")
    parser.add_argument(
        "--goal",
        default="Plan the selected agent task.",
        help="High-level manufacturing goal.",
    )
    parser.add_argument("--workflow-mode", choices=["direct", "process_then_robot"], default="direct")
    parser.add_argument("--capability", default="robot_path_planning", help="Capability for direct execution.")
    parser.add_argument("--agent-id", default=None, help="Optional registered agent to select explicitly.")
    parser.add_argument("--input-json", default="{}", help="Additional capability-specific input JSON object.")
    parser.add_argument("--unity-preview", action="store_true", help="Wait for local Unity playback for robot planning.")
    parser.add_argument("--part-id", default="skku/workpiece_01", help="Part ID to plan for.")
    parser.add_argument("--source", default=None, help="Optional robot source object. Defaults to pilot.default_source.")
    parser.add_argument("--requested-machine-id", default=None, help="Optional CAU machine override.")
    parser.add_argument("--destination", default=None, help="Destination object for direct robot planning.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the full JSON response.")
    args = parser.parse_args(argv)

    payload: dict[str, Any] = {
        "global_task_id": args.global_task_id or f"TASK-{int(time.time())}",
        "goal": args.goal,
        "part_id": args.part_id,
        "workflow_mode": args.workflow_mode,
        "capability": args.capability,
        "metadata": {"unity_preview": args.unity_preview},
    }
    if args.source:
        payload["source"] = args.source
    if args.requested_machine_id:
        payload["requested_machine_id"] = args.requested_machine_id
    if args.destination:
        payload["destination"] = args.destination
    elif args.workflow_mode == "direct" and args.capability == "robot_path_planning" and not args.requested_machine_id:
        payload["destination"] = "skku/work_table_01"
    if args.workflow_mode == "direct" and args.capability == "robot_path_planning" and not args.source:
        payload["source"] = "skku/workpiece_01"
    if args.agent_id:
        payload["agent_id"] = args.agent_id
    try:
        payload["input"] = json.loads(args.input_json)
        if not isinstance(payload["input"], dict):
            raise ValueError("--input-json must be an object")
    except (ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    response = _post_json(args.url, payload)
    if args.pretty:
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        _print_summary(response)
    return 0


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


def _print_summary(response: dict[str, Any]) -> None:
    print(f"global_task_id: {response.get('global_task_id')}")
    print(f"status: {response.get('status')}")

    tasks = response.get("tasks")
    if isinstance(tasks, dict):
        print("tasks:")
        for task_id, task in tasks.items():
            if isinstance(task, dict):
                print(
                    f"  - {task_id}: {task.get('capability')} "
                    f"via {task.get('assigned_agent_id')} -> {task.get('status')}"
                )

    artifacts = response.get("artifacts")
    if isinstance(artifacts, dict):
        process_plan = artifacts.get("process_plan")
        robot_plan = artifacts.get("robot_plan")
        if isinstance(process_plan, dict):
            print(f"process_plan: {process_plan.get('plan_id')} -> {process_plan.get('machine_id')}")
        if isinstance(robot_plan, dict):
            path = robot_plan.get("path")
            path_id = path.get("path_id") if isinstance(path, dict) else None
            print(f"robot_plan: {robot_plan.get('plan_id')} -> {path_id}")

    errors = response.get("errors")
    if errors:
        print("errors:")
        print(json.dumps(errors, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
