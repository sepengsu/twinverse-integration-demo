# Super Agent

기관별 에이전트를 A2A로 조정하고, 각 에이전트가 독립 MCP 서버에 연결하도록 구성하는 프로젝트입니다.

현재 구현된 범위는 Phase 1, Phase 2, Phase 3입니다.

- CAU Dummy Agent: `process_planning`, `equipment_selection`, `process_information`
- SKKU Robot Agent: `robot_path_planning`, `robot_state_query`, `path_validation`
- Supervisor Agent: `manufacturing_pilot_workflow`
- 통신: 공식 A2A SDK REST transport
- 기본 포트: Supervisor `9000`
- 기본 포트: CAU `9002`
- 기본 포트: SKKU `9001`
- SKKU Agent는 Twinverse FastMCP `http://127.0.0.1:8000/mcp`를 감싸며, Phase 2에서는 plan-only 동작만 허용합니다.

## 소스 구조

```text
src/
  agent_shared/             # 공유 설정·A2A 형식
  cau/                      # CAU 공정 A2A Agent
  skku/                     # 기존 SKKU 로봇 A2A Agent
  unist/                    # UNIST 연동 자료
    unity/A2APathPreview.cs
  super_agent/
    supervisor/             # Supervisor 실행·A2A·대시보드
      workflow.py            # 요청·작업 순서·상태 저장
      requests.py            # Agent 요청 생성·CAU→SKKU 변환
      registry.py            # 기능별 Agent 주소 선택
  ui/                       # Supervisor가 제공하는 웹 대시보드
```

UNIST A2A Agent는 아직 구현되지 않았습니다. `src/unist`에는 기존 Unity 연동 스크립트만 옮겼습니다.

Supervisor의 핵심 흐름은 `app.py`(서버 구성) → `executor.py`(실행) → `workflow.py`(상태)입니다. 기관별 요청 형식은 `requests.py`, Agent 주소 선택은 `registry.py`에서 확인할 수 있습니다.

기관·역할별로 처음 읽을 문서: [CAU](src/cau/README.md) · [SKKU](src/skku/README.md) · [UNIST Unity](src/unist/README.md) · [Supervisor](src/super_agent/README.md). 각 문서에 담당 코드, 실행, PC 분리 범위와 확인 방법을 적었습니다.

기관별 README는 단독으로도 핵심 실행 방법을 읽을 수 있습니다. `docs/`와 `deployment/`는 현재 `.gitignore` 대상이므로 저장소를 Git으로 전달할 때 해당 폴더의 상세 문서까지 필요하면 별도로 포함해야 합니다.

Python 서비스는 각각 `python -m cau.a2a_server`, `python -m skku.app`, `python -m super_agent.supervisor`로 실행합니다. `agent_shared`는 공통 모듈이며 서버가 아닙니다. `src/unist`는 현재 Unity 프로젝트에서 사용하는 C# 스크립트라 단독 Python 실행 명령이 없습니다.

타기관 PC에 `tools/` 전체를 복사할 필요는 없습니다. CAU에는 `deployment/cau-process-agent`에서 만든 배포 ZIP만 전달하면 됩니다. 현재 Twinverse 시연 환경을 그대로 재현할 때만 `tools/run_twinverse_http.py`와 `tools/twinverse_anchor_patch.py`를 함께 사용합니다. 그 밖의 `tools`는 이 PC의 빌드·촬영·검증 보조 파일입니다.

## 설정

기본 설정은 `config/agents.yaml`에서 읽습니다. 값은 다음 우선순위로 병합됩니다.

1. 운영체제 환경변수
2. `.env`
3. YAML
4. 코드 기본값

```powershell
Copy-Item .env.example .env
uv run python -m agent_shared.config
```

다른 YAML이나 `.env` 파일을 사용할 수도 있습니다.

```powershell
$env:SUPER_AGENT_CONFIG_FILE = "config/production.yaml"
$env:SUPER_AGENT_ENV_FILE = ".env.production"
uv run python -m agent_shared.config
```

환경변수는 `SUPER_AGENT_` 접두사와 `__` 중첩 구분자를 지원합니다.

```text
SUPER_AGENT_SUPERVISOR__PORT=9100
SUPER_AGENT_AGENTS__SKKU_ROBOT__MCP__URL=http://127.0.0.1:8000/mcp
SUPER_AGENT_AGENTS__CAU_PROCESS__ENABLED=true
```

자주 사용하는 주소에는 짧은 별칭도 사용할 수 있습니다.

```text
SKKU_AGENT_URL=http://127.0.0.1:9001
TWINVERSE_MCP_URL=http://127.0.0.1:8000/mcp
CAU_AGENT_URL=http://127.0.0.1:9002
```

Twinverse MCP Client Agent와 같은 supervisor LLM 환경변수도 지원합니다.

```text
SUPERVISOR_AGENT_MODEL="openrouter:gemini-3-flash-preview"
SUPERVISOR_AGENT_KWARGS='{"reasoning_effort":"minimal"}'
```

실제 LLM-backed planner/client를 켤 때 provider key는 로컬 `.env` 또는 OS 환경변수에만 둡니다.

```text
OPENROUTER_API_KEY=...
OPENAI_API_KEY=...
```

Twinverse의 MQTT, Kafka, Unity 설정과 인증정보는 Twinverse 프로젝트에서 관리하고, 이 프로젝트에는 MCP 주소만 전달하는 구성을 권장합니다.

## CAU Dummy A2A 서버 실행

```powershell
uv sync
uv run python -m cau.a2a_server
```

기본 서버 주소는 `http://127.0.0.1:9002`이며 Agent Card는 `http://127.0.0.1:9002/.well-known/agent-card.json`에서 확인할 수 있습니다.

## SKKU Robot A2A 서버 실행

Twinverse FastMCP 서버를 먼저 실행합니다.

```powershell
cd ..\twinverse
uv run --package twinverse-mcp-server twinverse-mcp-server http
```

그 다음 이 프로젝트에서 SKKU A2A Agent를 실행합니다.

```powershell
uv run python -m skku.app
```

기본 서버 주소는 `http://127.0.0.1:9001`이며 Agent Card는 `http://127.0.0.1:9001/.well-known/agent-card.json`에서 확인할 수 있습니다.

Phase 2의 SKKU Agent는 `plan_only`만 지원합니다. `start_path`, `real_start`, `move_tcp*`, 물리 robot motion, gripper actuation은 자동 호출하지 않습니다.

## Supervisor 실행

CAU Agent, SKKU Agent가 떠 있는 상태에서 Supervisor를 실행합니다.

```powershell
uv run python -m super_agent.supervisor.app
```

기본 서버 주소는 `http://127.0.0.1:9000`입니다.

브라우저에서 `http://127.0.0.1:9000/dashboard`를 열면 에이전트 연결 상태, 작업 실행, 실제 A2A 요청·응답 타임라인, JSON 상세와 결과물을 확인할 수 있습니다. 실행 중인 작업도 실시간으로 갱신되며 통신 기록을 JSON으로 내보낼 수 있습니다. 사용법은 `docs/dashboard.md`에 있습니다.

대시보드의 **Unity에서 경로 확인·재생**을 선택하면 공정 계획과 경로 발행 이후 Unity의 작업별 명령 승인과 로컬 재생 완료까지 기다립니다. 출발·상승·접근·도착 네 타깃을 만들며, 이번 작업의 명령 ID 5개와 실제 타깃 수 4개를 대조합니다. 관절 해석이 가능하면 Unity의 타깃 로봇을 로컬에서 재생하고, 불가능하면 TCP 경로 표시로 구분합니다. 충돌 검사 기능은 아직 없으므로 결과의 `collision_free`는 `null`, `collision_check`는 `not_performed`입니다. 설치와 시연 절차는 `docs/dashboard.md`의 Unity 절을 참고하세요.

REST entry point:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:9000/workflow `
  -ContentType "application/json" `
  -Body '{
    "global_task_id": "TASK-001",
    "goal": "Plan a robot path directly.",
    "part_id": "skku/workpiece_01",
    "source": "skku/workpiece_01",
    "destination": "skku/work_table_01"
  }'
```

Supervisor 자체도 A2A Agent로 동작하며, Agent Card는 `http://127.0.0.1:9000/.well-known/agent-card.json`에서 확인할 수 있습니다.

Phase 3 상세 문서는 `docs/phase3_supervisor_integration.md`에 있습니다.

## 전체 local stack 실행

개별 서버를 따로 켜지 않고 Phase 3 stack을 한 번에 구동하려면 다음을 사용합니다.

```powershell
uv run python -m super_agent.supervisor.run_stack
```

실제 Twinverse FastMCP까지 함께 띄우려면 sibling `..\twinverse` repo가 있는 상태에서 다음을 사용합니다.

```powershell
uv run python -m super_agent.supervisor.run_stack --start-twinverse
```

실행 중인 Supervisor에 workflow를 보내려면 별도 터미널에서 다음을 실행합니다.

```powershell
uv run python -m super_agent.supervisor.send_workflow --global-task-id TASK-001 --pretty
```

전체 stack 실행과 sample workflow 요청을 한 번에 수행할 수도 있습니다.

```powershell
uv run python -m super_agent.supervisor.run_stack --start-twinverse --run-sample
```

테스트:

```powershell
uv run python -m unittest discover -s tests -v
```

## 실행 방식 선택

기본값은 `workflow_mode: "direct"`입니다. 대시보드에서 등록된 Agent와 기능을 선택하면 해당 기능을 단독 실행합니다. 공정 계획이나 CAU 접속은 로봇 경로 직접 실행의 선행 조건이 아닙니다. 로봇 경로의 처음 선택된 Agent는 SKKU이며, 다른 기능으로 바꿀 수 있습니다.

API는 `capability`(기본 `robot_path_planning`), 선택적 `agent_id`, 기능별 `input` JSON을 지원합니다. 로봇 기능의 `input`은 `RobotTaskInput`, CAU 기능은 해당 요청 모델의 업무 필드입니다. 그 외 등록된 기능의 `input`은 A2A 요청 본문으로 전달하며, 스키마·작업 ID·기능 필드는 Supervisor가 설정합니다. 기관의 실제 입력 규격에 맞게 값을 제공해야 합니다. 결과는 Agent가 반환한 Artifact 이름으로 저장합니다. 같은 기능을 여러 기관이 제공하면 `agent_id`로 지정합니다.

기존 공정 계획 → 로봇 경로 방식은 `workflow_mode: "process_then_robot"`를 명시하면 사용할 수 있습니다. 대시보드의 실행 방식 선택에도 남아 있습니다. Unity 승인·재생은 로봇 경로 계획에서만 선택 가능하며, 로봇 요청은 두 모드 모두 `plan_only`로 유지합니다.

```powershell
# 직접 로봇 경로 실행 (기본)
uv run python -m super_agent.supervisor.send_workflow --agent-id skku_robot --pretty

# 중앙대 정보 조회만 실행
uv run python -m super_agent.supervisor.send_workflow --capability process_information --agent-id cau_process --pretty

# 기존 기관 협업 순서 선택
uv run python -m super_agent.supervisor.send_workflow --workflow-mode process_then_robot --source skku/workpiece_01 --requested-machine-id skku/work_table_01 --pretty
```
