# SKKU 로봇 경로 Agent

이 폴더는 성균관대 로봇 기능을 **독립 A2A 서버**로 제공합니다. 서버가 Twinverse FastMCP에 연결해 경로를 계획·발행합니다. 현재 동작은 `plan_only`이며 실물 로봇의 이동 시작 명령을 보내지 않습니다.

## 한눈에 보는 흐름

```text
Supervisor → A2A HTTP/JSON → SKKU 서버(:9001) → SkkuA2AExecutor
                                          → TwinverseMcpBackend
                                          → Twinverse FastMCP(:8000/mcp)
                                          → 경로 발행 결과 Artifact
```

| 기능(`capability`) | 하는 일 | 반환 결과 |
| --- | --- | --- |
| `robot_path_planning` | 출발·목적 개체를 기준으로 경로 계획 발행 | `robot_plan` |
| `robot_state_query` | Twinverse 개체와 콘솔 상태 조회 | `robot_state` |
| `path_validation` | 요청 형식과 계획 전용 조건의 검증 상태 반환 | `path_validation` |

경로 계획은 **출발 → 상승 → 접근 → 도착** 네 타깃을 만듭니다. `manage_path`의 `make_path`, `make_object_anchor_target`, `connect_targets`만 사용합니다. MCP의 발행 성공은 Unity의 명령 승인이나 로봇의 이동 완료를 뜻하지 않습니다. 충돌 검사기는 없으므로 `collision_free`는 `null`, `collision_check`는 `not_performed`입니다.

## 파일을 어디서 보면 되나요?

| 파일 | 역할 |
| --- | --- |
| [`app.py`](app.py) | Agent Card, `/health`, A2A HTTP 경로, 서버 실행 |
| [`executor.py`](executor.py) | A2A 요청을 기능별 백엔드 호출과 결과 상태로 연결 |
| [`models.py`](models.py) | 로봇 요청·경로·검증 결과 형식 |
| [`backend.py`](backend.py) | 백엔드 인터페이스와 오류 종류 |
| [`backends/twinverse_mcp.py`](backends/twinverse_mcp.py) | FastMCP 호출, 경로 발행, 안전 허용 목록 |
| [`prompts.py`](prompts.py) | 허용·차단할 경로 동작 이름 |

처음 읽을 때는 `app.py` → `executor.py` → `backends/twinverse_mcp.py` 순서가 편합니다.

## 이 저장소에서 실행

Twinverse FastMCP 서버를 먼저 준비합니다. 일반 실행 명령은 최상위 [`../../README.md`](../../README.md)의 **SKKU Robot A2A 서버 실행** 절을 보세요. 이 PC의 Unity 연동 시연 환경은 별도 보정 래퍼가 필요할 수 있으므로 [`../../docs/dashboard.md`](../../docs/dashboard.md)의 **Unity 승인과 로컬 시뮬레이션** 절을 따릅니다.

그다음 **이 저장소 최상위 폴더**에서 SKKU 서버를 켭니다.

```powershell
uv sync
uv run python -m skku.app
```

```powershell
Invoke-RestMethod http://127.0.0.1:9001/health
Invoke-RestMethod http://127.0.0.1:9001/.well-known/agent-card.json
```

`/health`가 `ok`여도 Twinverse MCP 또는 Unity가 준비됐다는 뜻은 아닙니다. 실제 경로 요청까지 확인해야 합니다. Supervisor가 켜져 있다면 대시보드에서 **성균관대 Agent → 로봇 경로 계획**을 선택합니다. 기본 주소는 [`../../config/agents.yaml`](../../config/agents.yaml)의 `agents.skku_robot.a2a`와 `agents.skku_robot.mcp`에 있습니다.

## 다른 PC에서 협업할 때

Supervisor에서 SKKU A2A 서버로 접속할 수 있도록 `SKKU_AGENT_URL` 또는 `agents.skku_robot.a2a.base_url`을 실제 LAN/VPN 주소로 지정합니다. SKKU 서버가 사용하는 `TWINVERSE_MCP_URL`은 **SKKU 서버가 실행되는 PC에서 접근 가능한** MCP 주소여야 합니다. 서버 수신 주소(`a2a.host`)는 외부 연결을 받을 수 있게 지정하고, Agent Card의 `base_url`은 Supervisor가 접근할 수 있는 주소로 설정합니다. `127.0.0.1`은 각 PC 자신을 가리킵니다.

SKKU PC의 예시 주소가 `192.168.10.30`일 때는 다음처럼 실행합니다. 실제 IP와 MCP 주소로 바꾸고 Supervisor PC에서 SKKU PC의 TCP 9001 접속을 허용합니다.

```powershell
# SKKU PC: 저장소 최상위 폴더에서
$env:SKKU_AGENT_URL = 'http://192.168.10.30:9001'
$env:TWINVERSE_MCP_URL = 'http://127.0.0.1:8000/mcp'  # MCP가 같은 PC에 있을 때
uv run python -m skku.app --host 0.0.0.0

# Supervisor PC: 자신의 .env에 같은 URL을 기록하거나 실행 전에 설정
$env:SKKU_AGENT_URL = 'http://192.168.10.30:9001'
Invoke-RestMethod http://192.168.10.30:9001/health
```

현재 저장소에는 CAU와 같은 SKKU 전용 원격 배포 ZIP이 없습니다. 서버 자체에 인증·TLS도 없으므로 PC 간 연결은 접근을 제한한 사설망/VPN에서 구성해야 합니다.

## 안전 범위와 Unity

`execution_mode`는 `plan_only`만 받습니다. [`backends/twinverse_mcp.py`](backends/twinverse_mcp.py)에서 실행 동작을 차단합니다. 경로 발행 뒤 Unity에서 실제로 받은 명령과 네 타깃을 확인하려면 Supervisor 대시보드의 **Unity에서 경로 확인·재생**을 선택합니다. 그 결과는 별도 `unity_preview` Artifact로 남습니다. Unity 연동 스크립트와 준비 방법은 [UNIST 안내](../unist/README.md)를 보세요. 이 로컬 재생도 실물 로봇 실행이나 충돌 검증은 아닙니다.

## 관련 문서와 확인

- [전체 구조와 안전 경계](../../docs/architecture_graph.md)
- [대시보드·Unity 시연](../../docs/dashboard.md)
- 테스트: 저장소 최상위 폴더에서 `uv run python -m unittest discover -s tests -v`.
