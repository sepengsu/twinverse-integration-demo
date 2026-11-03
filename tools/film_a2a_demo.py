"""Film the actual dashboard workflow and live Unity window using local APIs."""
from __future__ import annotations

import json
import re
import time
import uuid
import urllib.request

from obs_recording import ObsClient, OUTPUT


def cue(action):
    identifier = str(uuid.uuid4())
    (OUTPUT / "browser-command.json").write_text(json.dumps({"id": identifier, "action": action}), encoding="utf-8")
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        time.sleep(.25)
        file = OUTPUT / "browser-ack.json"
        if not file.exists():
            continue
        try:
            result = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if result.get("id") != identifier:
            continue
        if result["status"] != "ok":
            raise RuntimeError(result.get("error", result))
        return
    raise TimeoutError(f"Browser cue timed out: {action}")


def main():
    client = ObsClient()
    started = False
    result = {"scope": {"web": True, "unity": True, "kafka": False, "cau_backend": "dummy", "skku_mode": "plan_only_with_local_preview", "collision_check": "not_performed", "audio": False}}
    phases = []
    recording_started_at = 0

    def evidence():
        with urllib.request.urlopen("http://127.0.0.1:9000/dashboard/api/snapshot", timeout=3) as response:
            return json.load(response)

    def hold(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            time.sleep(min(1, max(0, until - time.monotonic())))

    def scene(caption, action, seconds):
        client.call("SetInputSettings", inputName="Recording Caption", inputSettings={"text": caption}, overlay=True)
        phases.append({"seconds": round(time.monotonic() - recording_started_at, 2), "action": action, "caption": caption})
        cue(action)
        print(f"Recording phase: {action}", flush=True)
        hold(seconds)

    try:
        cue("overview")
        if not evidence().get("unity_runtime", {}).get("connected"):
            raise RuntimeError("Unity runtime is not connected")
        client.call("SetRecordDirectory", recordDirectory=str(OUTPUT))
        client.call("SetCurrentProgramScene", sceneName="A2A Demo")
        client.call("StartRecord")
        started = True
        recording_started_at = time.monotonic()
        print("Recording started", flush=True)
        scene("1. 왼쪽은 작업별 실행 단계, 오른쪽은 실행 중인 Unity 장면입니다.", "overview", 8)
        scene("2. 출발 개체와 목적 설비를 지정하고 Unity 경로 확인·재생을 선택합니다.", "launch-form", 5)
        scene("3. 새 작업 실행 → 실제 A2A 통신 → 네 타깃 발행 → Unity 승인·재생", "start", 0)
        task_id = json.loads((OUTPUT / "filmed-task.json").read_text(encoding="utf-8"))["global_task_id"]
        deadline = time.monotonic() + 95
        while time.monotonic() < deadline:
            snapshot = evidence()
            workflow = next(row for row in snapshot["workflows"] if row["global_task_id"] == task_id)
            if workflow["status"] in {"completed", "failed", "input_required"}:
                break
            hold(.5)
        else:
            raise RuntimeError("Filmed Unity preview timed out")
        (OUTPUT / "filmed-workflow.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUTPUT / "filmed-events.json").write_text(json.dumps([event for event in snapshot["events"] if event["global_task_id"] == task_id], ensure_ascii=False, indent=2), encoding="utf-8")
        if workflow["status"] != "completed":
            raise RuntimeError(f"Filmed workflow did not complete: {workflow['status']}")
        result["workflow_id"] = workflow["global_task_id"]
        result["workflow_status"] = workflow["status"]
        result["artifact_names"] = list(workflow["state"]["artifacts"])
        result["unity"] = workflow["unity"]
        scene("4. 이번 작업의 명령 5/5, 타깃 4/4, Unity 재생 100%를 확인한 뒤 완료됩니다.", "execution", 7)
        scene("5. Supervisor → 중앙대: 실제 A2A 요청 JSON을 확대해 확인합니다.", "cau-request", 7)
        scene("6. 공정 계획 결과를 별도로 선택해 확인합니다.", "process-plan", 7)
        scene("7. 로봇 경로는 출발·상승·접근·도착의 네 타깃으로 구성됩니다.", "robot-plan", 7)
        scene("8. 명령 발행 성공과 충돌 검증을 구분합니다. collision_free는 미확인입니다.", "validation", 8)
        scene("9. Unity가 반환한 이번 작업의 승인 ID와 실제 재생 결과를 확인합니다.", "unity-result", 8)
        scene("시연 완료  |  A2A → MCP → MQTT → Unity 승인 → 로컬 경로 재생", "execution", 7)
        result["success"] = True
    except Exception as error:
        result["success"] = False
        result["error"] = str(error)
        raise
    finally:
        if started:
            result["recording"] = client.call("StopRecord")["outputPath"]
            print("Recording stopped: " + result["recording"], flush=True)
        (OUTPUT / "recording-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUTPUT / "recording-phases.json").write_text(json.dumps(phases, ensure_ascii=False, indent=2), encoding="utf-8")
        client.close()


if __name__ == "__main__":
    main()
