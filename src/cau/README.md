# CAU 공정 Agent

이 폴더는 중앙대의 공정 기능을 **독립 A2A 서버**로 제공하는 코드입니다. 현재 업무 데이터는 샘플 YAML에서 읽는 Dummy 구현입니다. 공정 계획을 반환하지만 실제 설비를 제어하거나 공정 시간을 측정하지는 않습니다.

## 한눈에 보는 흐름

```text
Supervisor → A2A HTTP/JSON → CAU 서버(:9002) → CauA2AExecutor
                                         → DummyCauBackend → 샘플 YAML
                                         ← 공정 결과 Artifact
```

| 기능(`capability`) | 입력 | 반환 결과 |
| --- | --- | --- |
| `process_planning` | 부품 ID, 선택적 설비 ID·제약 | `process_plan`: 설비와 공정 단계 |
| `equipment_selection` | 부품 ID, 공정 종류·제약 | `equipment_selection`: 후보 설비 |
| `process_information` | 조회 주제 | `process_information`: 지원 기능·공정·소재 |

요청에는 `global_task_id`(전체 작업 연결), `request_id`(개별 요청 식별), `capability`가 들어갑니다. 입력과 결과 형식은 [`contracts.py`](contracts.py)에 정의되어 있습니다.

## 파일을 어디서 보면 되나요?

| 파일 | 역할 |
| --- | --- |
| [`a2a_server.py`](a2a_server.py) | Agent Card, `/health`, A2A HTTP 경로, 서버 실행 |
| [`a2a_executor.py`](a2a_executor.py) | A2A 메시지 해석, 기능별 호출, 결과·오류 상태 반환 |
| [`contracts.py`](contracts.py) | 요청과 결과의 Pydantic 형식 |
| [`backend.py`](backend.py) | 실제 CAU 구현이 맞춰야 할 세 메서드 |
| [`dummy_backend.py`](dummy_backend.py) | 샘플 YAML을 사용하는 현재 구현 |
| [`../../config/fixtures/cau_process_plan.yaml`](../../config/fixtures/cau_process_plan.yaml) | 더미 공정·설비 데이터 |

처음 읽을 때는 `a2a_server.py` → `a2a_executor.py` → `dummy_backend.py` 순서가 편합니다.

## 이 저장소에서 실행

아래 명령은 **저장소 최상위 폴더**에서 실행합니다.

```powershell
uv sync
uv run python -m cau.a2a_server
```

별도 터미널에서 연결을 확인합니다.

```powershell
Invoke-RestMethod http://127.0.0.1:9002/health
Invoke-RestMethod http://127.0.0.1:9002/.well-known/agent-card.json
```

`/health`에 `status: ok`가 나오고 Agent Card에 위 세 기능이 보여야 합니다. Supervisor까지 실행했다면 웹 대시보드에서 **중앙대 Agent**를 선택해 기능을 호출할 수 있습니다. 기본 설정은 [`../../config/agents.yaml`](../../config/agents.yaml)의 `agents.cau_process`입니다.

## 다른 PC에서 협업할 때

CAU PC에는 CAU 서버만 배포하면 됩니다. Windows 배포본 설치, 광고할 IP, 방화벽, Supervisor에서의 주소 등록은 [`../../deployment/cau-process-agent/INSTALL_KO.md`](../../deployment/cau-process-agent/INSTALL_KO.md)를 따르세요. 핵심은 **CAU가 Agent Card에 알리는 `base_url`을 Supervisor PC가 접속 가능한 LAN/VPN 주소로 설정하는 것**입니다. 다른 PC의 `127.0.0.1`은 CAU PC가 아닙니다.

예를 들어 CAU PC가 `192.168.10.20`이면 그 PC에서 서버를 외부 연결이 가능한 주소로 실행하고 Agent Card에 `http://192.168.10.20:9002`를 광고합니다. Supervisor PC의 `CAU_AGENT_URL`도 같은 주소로 설정합니다. 두 PC에서 `/health`와 `/.well-known/agent-card.json`을 확인한 뒤, Supervisor에서는 `--workflow-mode process_then_robot`를 명시해 CAU→SKKU 협업 흐름을 실행합니다. `run_stack`은 로컬 CAU도 함께 켜므로 원격 CAU 구성에는 개별 서버 실행 명령을 사용합니다.

저장소 소스를 CAU PC에 복사해 실행하는 경우의 예시는 다음과 같습니다. 실제 IP로 바꾸고 Supervisor PC에서 CAU PC의 TCP 9002 접속을 허용해야 합니다.

```powershell
# CAU PC: 저장소 최상위 폴더에서
$env:CAU_AGENT_URL = 'http://192.168.10.20:9002'
uv run python -m cau.a2a_server --host 0.0.0.0

# Supervisor PC: 자신의 .env에 같은 URL을 기록하거나 실행 전에 설정
$env:CAU_AGENT_URL = 'http://192.168.10.20:9002'
Invoke-RestMethod http://192.168.10.20:9002/health
```

현재 서버 자체에는 인증·TLS가 없습니다. 원격 연결은 접근을 제한한 사설망/VPN에서 사용합니다.

## 실제 CAU 백엔드로 교체하려면

[`backend.py`](backend.py)의 `CauBackend`가 요구하는 `create_process_plan`, `select_equipment`, `get_process_information`을 실제 CAU 서비스에 맞게 구현합니다. 반환값은 [`contracts.py`](contracts.py)의 Artifact 형식을 따라야 합니다. 그런 다음 [`a2a_server.py`](a2a_server.py)에서 `DummyCauBackend`를 생성하는 부분을 새 백엔드로 바꿉니다. 현재 `backend.type` 설정만 바꿔서는 자동으로 교체되지 않습니다.

입력 누락은 A2A `input_required`, 지원하지 않는 기능은 거절, 백엔드 오류는 실패로 반환합니다. 더미 결과는 프로세스 메모리에서 `request_id`별로 재사용되며 서버를 다시 켜면 캐시가 사라집니다.

## 관련 문서와 확인

- [Phase 1 CAU 구현 설명](../../docs/phase1_cau_a2a_implementation.md): 계약과 A2A 상태 흐름. 당시 작성한 역사 문서이므로 현재 실행 명령은 이 README를 따릅니다.
- [전체 실행 방법](../../README.md): Supervisor와 다른 기관을 함께 실행할 때.
- 테스트: 저장소 최상위 폴더에서 `uv run python -m unittest discover -s tests -v`.
