# Supervisor Agent

Supervisor는 사용자 요청을 받아 CAU·SKKU A2A Agent에 위임하고 결과를 한 작업 상태로 묶습니다. 웹 UI도 이 서버에서 제공합니다. 기본 포트는 `9000`입니다.

## 실행 흐름

```text
사용자 / 웹 UI → Supervisor(:9000) → 기능별 Agent 주소 선택
                                  → CAU 또는 SKKU에 A2A 메시지
                                  ← Artifact를 검증·저장
                                  → 선택한 경우 Unity 로컬 재생 확인
```

기본 `direct` 모드는 선택한 Agent 기능 하나를 실행합니다. `process_then_robot` 모드는 CAU의 `process_plan`을 받은 뒤 그 설비 ID를 SKKU 로봇 요청의 목적지로 전달합니다. SKKU 요청은 두 모드 모두 `plan_only`입니다. 두 단계의 작업 상태와 결과는 `global_task_id`로 연결합니다.

현재 작업 순서는 `DeterministicPilotPlanner`가 정합니다. `config/agents.yaml`에 LLM 모델명이 설정되어 있지만 이 실행 흐름에서 LLM을 호출하지는 않습니다.

## 핵심 파일 지도

| 파일 | 무엇을 볼 수 있나요? |
| --- | --- |
| [`supervisor/app.py`](supervisor/app.py) | HTTP/A2A 서버, Agent Card, 의존 구성 |
| [`supervisor/executor.py`](supervisor/executor.py) | A2A 위임부터 결과 검증·Unity 선택까지의 실행 순서 |
| [`supervisor/workflow.py`](supervisor/workflow.py) | 요청 형식, 1~2단계 작업, 상태 저장 |
| [`supervisor/requests.py`](supervisor/requests.py) | 직접 요청과 CAU 공정 결과의 SKKU 입력 변환 |
| [`supervisor/registry.py`](supervisor/registry.py) | 기능을 제공하는 Agent 주소 선택 |
| [`supervisor/a2a_client.py`](supervisor/a2a_client.py) | 공식 A2A SDK로 하위 Agent 호출 |
| [`supervisor/dashboard.py`](supervisor/dashboard.py) | 웹 UI API, 연결 상태, Unity 브리지 경로 |
| [`supervisor/telemetry.py`](supervisor/telemetry.py) | 작업별 통신 기록과 민감 필드 가림 |
| [`supervisor/unity_preview.py`](supervisor/unity_preview.py) | Unity 작업별 승인·진행률 검증 |
| [`supervisor/kafka_events.py`](supervisor/kafka_events.py) | 선택적 Kafka **이벤트 복사** |

처음 읽을 때는 `app.py` → `executor.py` → `workflow.py` → `requests.py` 순서가 좋습니다. 웹 화면 자체는 [`../ui`](../ui)에 있습니다.

## 실행과 확인

명령은 **저장소 최상위 폴더**에서 실행합니다. CAU와 SKKU 서버를 각각 준비한 후:

```powershell
uv sync
uv run python -m super_agent.supervisor.app
```

같은 PC에서 세 A2A 서버를 한 번에 띄우려면 `uv run python -m super_agent.supervisor.run_stack`을 사용합니다. Twinverse FastMCP는 별도로 준비해야 합니다. 자세한 순서는 [최상위 README](../../README.md)의 **전체 local stack 실행** 절을 보세요.

```powershell
Invoke-RestMethod http://127.0.0.1:9000/health
```

웹 UI는 `http://127.0.0.1:9000/dashboard`입니다. Agent 연결, 실행 단계, 실제 A2A 요청·응답과 Artifact를 볼 수 있습니다. UI 사용법은 [대시보드 문서](../../docs/dashboard.md)를 보세요.

직접 로봇 경로 예시:

```powershell
uv run python -m super_agent.supervisor.send_workflow --agent-id skku_robot
```

CAU→SKKU 협업 예시:

```powershell
uv run python -m super_agent.supervisor.send_workflow --workflow-mode process_then_robot --source skku/workpiece_01 --requested-machine-id skku/work_table_01
```

## 설정·저장·Kafka

Agent 주소와 실행 설정은 [`../../config/agents.yaml`](../../config/agents.yaml)에 있습니다. 다른 PC의 CAU/SKKU를 호출한다면 `CAU_AGENT_URL`, `SKKU_AGENT_URL`에 Supervisor가 접근 가능한 주소를 넣습니다. 설정 우선순위와 `.env` 사용법은 [최상위 README](../../README.md)의 **설정** 절을 보세요.

작업 상태와 대시보드 기록은 현재 **프로세스 메모리**에 저장됩니다. 서버를 재시작하면 기록이 사라집니다. `kafka_events`는 기본적으로 꺼져 있습니다. 켜면 가려진 통신 이벤트를 Kafka에 복사하지만, Agent 위임은 계속 A2A HTTP/JSON으로 수행합니다. Kafka는 작업 실행을 대신하는 Worker가 아닙니다.

Unity 재생은 선택 사항이며 [UNIST 안내](../unist/README.md)의 로컬 브리지가 필요합니다. Unity 재생 완료도 실물 로봇 완료를 의미하지 않습니다.

## 관련 문서와 검증

- [Phase 3 Supervisor 구현과 실패 전파](../../docs/phase3_supervisor_integration.md)
- [전체 구조 그림](../../docs/architecture_graph.md)
- 테스트: 저장소 최상위 폴더에서 `uv run python -m unittest discover -s tests -v`.
