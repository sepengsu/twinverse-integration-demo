"""Record the default direct task and the optional institution collaboration."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime

from film_a2a_demo import cue
from obs_recording import ObsClient, OUTPUT


def snapshot():
    with urllib.request.urlopen("http://127.0.0.1:9000/dashboard/api/snapshot", timeout=4) as response:
        return json.load(response)


def main():
    focus_cau = "--cau-focus" in sys.argv
    single_run = "--single-run" in sys.argv
    slow_tempo = "--slow-tempo" in sys.argv
    client = ObsClient()
    started = False
    phases = []
    result = {"success": False, "scenarios": [], "scope": {
        "web": True, "unity": True, "audio": False, "kafka": False,
        "cau_backend": "dummy", "skku_mode": "plan_only_with_local_joint_simulation",
        "collision_check": "not_performed"}}
    start_time = 0

    def scene(caption, action, seconds):
        client.call("SetInputSettings", inputName="Recording Caption", inputSettings={"text": caption}, overlay=True)
        phases.append({"seconds": round(time.monotonic() - start_time, 2), "caption": caption, "action": action})
        cue(action)
        print(f"Recording phase: {action}", flush=True)
        time.sleep(seconds)

    def complete(name):
        task_id = json.loads((OUTPUT / "filmed-task.json").read_text(encoding="utf-8"))["global_task_id"]
        deadline = time.monotonic() + 95
        while time.monotonic() < deadline:
            trace = snapshot()
            row = next(row for row in trace["workflows"] if row["global_task_id"] == task_id)
            if row["status"] in {"completed", "failed", "input_required"}:
                break
            time.sleep(.3)
        else:
            raise RuntimeError(f"{name}: Unity replay timed out")
        events = [event for event in trace["events"] if event["global_task_id"] == task_id]
        (OUTPUT / f"{name}-workflow.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUTPUT / f"{name}-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        unity = row.get("unity", {})
        if row["status"] != "completed" or name != "cau" and unity.get("status") != "completed":
            raise RuntimeError(f"{name}: actual workflow or Unity did not complete")
        if name != "cau" and unity.get("playback_kind") != "joint_waypoint_simulation":
            raise RuntimeError(f"{name}: actual joint IK was not verified")
        if name != "cau" and (len(unity["accepted_command_ids"]) != 5 or unity["waypoint_count"] != 4):
            raise RuntimeError(f"{name}: unexpected actual command receipts or waypoints")
        requests = [event for event in events if event["kind"] == "http_request" and event["data"].get("method") == "POST"]
        if len(requests) != (1 if name in {"direct","cau"} else 2):
            raise RuntimeError(f"{name}: unexpected A2A request count")
        if name == "direct" and any(event.get("target") == "cau_process" for event in events):
            raise RuntimeError("Direct task unexpectedly delegated to CAU")
        if name == "cau" and (not row["state"]["artifacts"].get("process_plan") or any(event.get("target") == "skku_robot" for event in events)):
            raise RuntimeError("CAU standalone planning did not return its own process plan")
        process_plan = row["state"]["artifacts"].get("process_plan")
        handoff = next((event for event in events if event["kind"] == "payload_transformed"), None)
        robot_request = next((event for event in requests if event["target"] == "skku_robot"), None)
        if name == "collaboration":
            posted = next((part["data"] for part in robot_request["data"]["body"]["message"]["parts"] if "data" in part), {})
            if not process_plan or not handoff or posted.get("input",{}).get("destination") != process_plan["machine_id"]:
                raise RuntimeError("Actual CAU output did not match the SKKU request destination")
        if slow_tempo:
            stage_seconds = {}
            for agent in ["cau_process", "skku_robot"]:
                sent = next(event for event in requests if event["target"] == agent)
                received = next(event for event in events if event["kind"] == "task_received" and event["source"] == agent)
                elapsed = (datetime.fromisoformat(received["timestamp"]) - datetime.fromisoformat(sent["timestamp"])).total_seconds()
                if not 9.9 <= elapsed <= 13:
                    raise RuntimeError(f"{agent}: expected the requested 10-second demonstration stage, got {elapsed}s")
                stage_seconds[agent] = elapsed
            running = next(event for event in events if event["kind"] == "unity_running")
            finished = next(event for event in events if event["kind"] == "unity_completed")
            elapsed = (datetime.fromisoformat(finished["timestamp"]) - datetime.fromisoformat(running["timestamp"])).total_seconds()
            if not 35.5 <= elapsed <= 45:
                raise RuntimeError(f"Expected slower 36-second local joint replay, got {elapsed}s")
            result["demo_pacing"]["measured_agent_seconds"] = stage_seconds
            result["demo_pacing"]["measured_simulation_seconds"] = elapsed
            progress_reports = [event for event in events if event["kind"] == "unity_running"]
            if len(progress_reports) < 10 or max(event["data"]["progress"] for event in progress_reports) < .9:
                raise RuntimeError("Slow playback did not publish intermediate dashboard progress")
            result["demo_pacing"]["dashboard_progress_updates"] = len(progress_reports)
        result["scenarios"].append({"name": name, "workflow_id": task_id, "workflow_status": row["status"],
                                    "a2a_requests": len(requests), "unity": unity, "process_plan": process_plan})
        result.update(workflow_id=task_id, workflow_status=row["status"], unity=unity,
                      artifact_names=list(row["state"]["artifacts"]))
        (OUTPUT / "filmed-workflow.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUTPUT / "filmed-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Verified filmed scenario: {name}, " + ("actual CAU process plan" if name == "cau" else "commands=5/5, waypoints=4/4, actual joint replay"), flush=True)

    def wait_for_stage(predicate):
        task_id = json.loads((OUTPUT / "filmed-task.json").read_text(encoding="utf-8"))["global_task_id"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            trace = snapshot()
            row = next(row for row in trace["workflows"] if row["global_task_id"] == task_id)
            if row["status"] in {"failed", "input_required"}:
                raise RuntimeError("Filmed workflow failed while waiting for its next stage")
            if predicate(row):
                return
            time.sleep(.2)
        raise RuntimeError("Expected filmed stage did not start")

    try:
        cue("prepare-single" if single_run else "prepare-cau" if focus_cau else "prepare-direct")
        cue("live-follow")
        if not snapshot().get("unity_runtime", {}).get("connected"):
            raise RuntimeError("Unity runtime is not connected")
        client.call("SetRecordDirectory", recordDirectory=str(OUTPUT))
        client.call("SetCurrentProgramScene", sceneName="A2A Demo")
        if single_run:
            if snapshot()["workflows"]:
                raise RuntimeError("Single-run demonstration must begin with empty workflow history")
            client.call("SetInputSettings", inputName="Recording Caption", inputSettings={"text":"한 번 입력: 경로 생성 후 Unity Simulation으로 로봇을 수행합니다."}, overlay=True)
        client.call("StartRecord")
        started = True
        start_time = time.monotonic()
        if single_run:
            result["focus"] = "One 3-second input, one collaboration workflow, automatic Unity Simulation"
            phases.append({"seconds":0,"caption":"한 번 입력: 경로 생성 후 Unity Simulation으로 로봇을 수행합니다.","action":"submit-once"})
            cue("submit-once")
            if slow_tempo:
                result["demo_pacing"] = {"agent_stage_seconds":10,"simulation_seconds":36,"added_presentation_wait":True}
                scene("1. 중앙대 Agent  |  공정 계획·목적 설비 반환 / 시연 템포 10초", "live-follow", 0)
                wait_for_stage(lambda row: bool(row.get("state",{}).get("artifacts",{}).get("process_plan")))
                scene("2. 성균관대 Agent  |  경로·Path Target 생성 / 시연 템포 10초", "live-follow", 0)
                wait_for_stage(lambda row: row.get("unity",{}).get("status") in {"accepted","running","completed"})
                scene("3. Unity Simulation  |  네 경유점을 따라 로봇 관절을 36초 동안 천천히 재생", "live-follow", 0)
            else:
                scene("자동 실행  |  CAU 공정 계획 → SKKU 경로 생성 → Unity 로컬 관절 Simulation", "live-follow", 0)
            complete("collaboration")
            submitted = json.loads((OUTPUT / "submitted-requests.json").read_text(encoding="utf-8"))
            if len(submitted) != 1 or len(snapshot()["workflows"]) != 1:
                raise RuntimeError("Single-run demonstration submitted more than one workflow")
            result["single_input"] = json.loads((OUTPUT / "single-input.json").read_text(encoding="utf-8"))
            result["user_requests"] = len(submitted)
            scene("완료  |  입력 1회 · 이번 명령 5/5 승인 · 네 타깃 로봇 관절 Simulation 완료", "unity-result", 6)
            result["success"] = True
            return
        if focus_cau:
            result["focus"] = "CAU output and actual handoff to SKKU"
            scene("1. 중앙대 Agent의 공정 계획을 먼저 직접 실행합니다.", "overview", 5)
            scene("2. 부품과 목적 설비를 입력합니다. 현재 CAU 백엔드는 Dummy 예제입니다.", "launch-form", 5)
            cue("close-settings")
            scene("3. Supervisor → CAU 요청 → 공정 계획 응답 / Unity 재생은 요청하지 않습니다.", "start-cau", 0)
            complete("cau")
            scene("4. CAU 결과: 투입 30초 → 가공 300초 → 배출 30초 / 예제 예상 합계 360초", "process-plan", 10)
            scene("5. 공정 단계와 설비 정보가 실제 반환된 ProcessPlan에 들어 있습니다.", "process-operations", 7)
            scene("6. CAU가 받은 실제 부품·목적 설비 입력을 확인합니다.", "cau-request", 5)
            scene("7. 중앙대 응답: 공정 3개와 설비를 반환합니다. 물리 가공을 실행한 것은 아닙니다.", "cau-response", 5)
            scene("8. 협업 옵션으로 새 작업 실행: CAU 결과를 받아 SKKU 요청으로 연결합니다.", "prepare-pilot", 4)
            scene("9. 공정 계획 → 로봇 경로 요청 → Unity 승인·관절 재생", "start-pilot", 0)
            complete("collaboration")
            scene("10. CAU machine_id → Supervisor 변환 → SKKU destination / 동일 설비 전송 확인", "handoff", 8)
            scene("11. 실제 SKKU 전송 JSON에서도 같은 목적지 값을 확인합니다.", "skku-destination", 7)
            scene("12. 중앙대가 반환한 공정 계획은 로봇 재생 후에도 결과 카드에 남습니다.", "process-plan", 6)
            scene("13. 성균관대는 전달받은 목적지의 로봇 경로를 반환합니다.", "skku-response", 5)
            scene("14. 경로의 네 타깃과 Unity 재생 결과를 확인합니다.", "robot-plan", 5)
            scene("15. 충돌 검사는 미실행입니다. 명령 승인·관절 재생 결과와 구분합니다.", "validation", 5)
            scene("16. Unity 결과: 이번 명령 5/5 승인, 네 타깃 관절 시뮬레이션 완료", "unity-result", 5)
            scene("완료  |  CAU 공정 계획 → 실제 데이터 연결 → SKKU 경로 → Unity 로컬 관절 재생", "live-follow", 6)
            result["success"] = True
            return
        scene("1. 네트워크·현재 처리·최근 응답·실제 JSON을 한 화면에서 확인합니다.", "overview", 6)
        scene("2. 기본 모드: 성균관대 Agent의 로봇 경로 기능을 직접 실행합니다.", "launch-form", 5)
        cue("close-settings")
        scene("3. 실제 요청 전송 → 성균관대 응답 → Unity 승인·관절 시뮬레이션", "start-direct", 0)
        complete("direct")
        scene("4. 직접 실행 완료: 중앙대는 참여하지 않고 Unity가 재생 결과를 반환합니다.", "execution", 4)
        scene("5. 주황색 요청: Supervisor → 성균관대 / 실제 전송 JSON", "skku-request", 5)
        scene("6. 초록색 응답: 성균관대 → Supervisor / 실제 Task와 Artifact", "skku-response", 5)
        scene("7. 선택 옵션: 공정 계획 → 로봇 경로 순서의 기관 협업도 실행합니다.", "prepare-pilot", 4)
        scene("8. 중앙대 공정 계획 응답 → 성균관대 경로 계획 → Unity 관절 재생", "start-pilot", 0)
        complete("collaboration")
        scene("9. Supervisor → 중앙대: 공정 계획 요청의 발신자·수신자 확인", "cau-request", 5)
        scene("10. 중앙대 → Supervisor: 실제 공정 계획 응답 확인", "cau-response", 5)
        scene("11. Supervisor → 성균관대: 공정 결과를 연결한 로봇 경로 요청", "skku-request", 5)
        scene("12. 성균관대 → Supervisor: 최근 응답 Agent와 수신 시각 확인", "skku-response", 5)
        scene("13. 반환된 경로: 출발·상승·접근·도착의 네 타깃", "robot-plan", 5)
        scene("14. 충돌 검사는 미실행입니다. 발행·승인·재생 결과와 구분합니다.", "validation", 5)
        scene("15. Unity 결과: 이번 명령 5/5 승인, 타깃 4/4, 관절 재생 완료", "unity-result", 5)
        scene("완료  |  Agent 직접 실행 + 기관 협업 옵션 / 실제 A2A 통신과 Unity 재생", "live-follow", 6)
        result["success"] = True
    except Exception as exc:
        result["error"] = str(exc)
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
