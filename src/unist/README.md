# UNIST Unity 연동

현재 이 폴더에는 **UNIST 독립 A2A Agent가 없습니다.** [`unity/A2APathPreview.cs`](unity/A2APathPreview.cs)는 Twinverse Unity 장면에서 경로 명령을 확인하고 로컬 시뮬레이션을 재생하는 브리지입니다. 따라서 `python -m unist` 같은 실행 명령이나 UNIST 서버 포트는 아직 없습니다.

## 브리지가 하는 일

```text
SKKU 경로 발행 → Twinverse MQTT/Unity 장면
Supervisor(:9000) → Unity 작업 전달 → A2APathPreview.cs
Supervisor(:9000) ← 작업별 명령 승인·경유점·재생 결과
```

1. Unity가 Supervisor의 `/dashboard/api/unity/job`을 주기적으로 조회합니다.
2. 이번 작업에 대해 발행된 MQTT 명령 ID가 Unity에서 **정확히** 승인됐는지 확인합니다. 이전 작업의 누적 명령 수로 대체하지 않습니다.
3. Unity가 가진 실제 경유점 네 개를 확인한 뒤 로컬 타깃 로봇을 재생합니다.
4. `/dashboard/api/unity/report`에 승인·진행률·완료 또는 실패를 보고합니다.

재생 결과는 `joint_waypoint_simulation`(관절 재생) 또는 `tcp_path_preview`(TCP 경로 표시)로 구분됩니다. 실물 로봇 제어, 작업물 집기, 충돌 검사는 포함되지 않습니다.

## Unity 프로젝트에 적용

Twinverse UnityRuntime이 설치된 Unity 프로젝트의 `Assets` 아래에 [`unity/A2APathPreview.cs`](unity/A2APathPreview.cs)를 복사합니다. `SKKU_RobotRoom` 장면과 `SceneAuthorityController`, `TwinObjectBinding`, `UrdfRobotTwinAdapter`가 필요합니다. Supervisor와 Twinverse를 먼저 실행한 뒤 Unity에서 장면을 **Play**합니다. 브리지는 Play 시작 시 자동으로 생성됩니다.

대시보드 상단에 **Unity 연결됨**이 보이는지 확인한 뒤 로봇 경로 작업의 **Unity에서 경로 확인·재생**을 선택합니다. 승인된 명령 5/5, 계획·Unity 경유점 4/4, 재생 100%가 보이면 해당 작업의 로컬 시연이 끝난 것입니다. 세부 순서와 현재 시연 환경의 Twinverse 래퍼는 [대시보드 문서](../../docs/dashboard.md)의 **Unity 승인과 로컬 시뮬레이션** 절에 있습니다.

## PC 분리 범위

현재 C# 스크립트의 Supervisor 주소는 `http://127.0.0.1:9000`으로 고정되어 있고, Supervisor의 Unity 전용 API는 로컬 요청만 허용합니다. **UNIST PC를 별도 PC로 옮겨 바로 연결하는 구성은 아직 지원하지 않습니다.** PC를 분리할 때는 브리지 주소를 설정 가능하게 만들고, Unity 보고용 인증과 네트워크 접근 정책을 함께 설계해야 합니다.

## 파일과 검증

| 위치 | 역할 |
| --- | --- |
| [`unity/A2APathPreview.cs`](unity/A2APathPreview.cs) | 작업 조회, 명령 승인 확인, 경로 재생, 결과 보고 |
| [`../super_agent/supervisor/unity_preview.py`](../super_agent/supervisor/unity_preview.py) | Supervisor의 작업 대기열과 보고 검증 |
| [`../../docs/dashboard.md`](../../docs/dashboard.md) | Unity 시연 절차와 제한사항 |

Python 측 보고 검증은 저장소 최상위 폴더에서 `uv run python -m unittest discover -s tests -p test_unity_preview.py -v`로 확인합니다. 이 테스트는 Unity Editor의 실제 Play 동작을 대체하지 않습니다.
