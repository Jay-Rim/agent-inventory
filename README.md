# Agent Inventory MVP

PC 또는 서버에서 **소스·실행 프로세스·예약 작업 → 근거가 있는 Agent Card → 변경 초안 → 담당자 승인**을 연결하는 로컬 수집기입니다. 별도 API 키나 DB 서버 없이 실행하며, Windows 프로세스의 작업 디렉터리 수집에 psutil을 사용합니다.

## 바로 실행

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\Activate.ps1
python -m agent_inventory scan
python -m agent_inventory list
python -m agent_inventory watch --interval 30
```

`scan`은 한 번 실행 후 종료합니다. `watch`는 30초마다 소스와 호스트 정보를 다시 확인합니다. 종료는 Ctrl+C입니다. 결과는 `.inventory/index.html`을 브라우저로 열어 확인합니다. 페이지는 새로고침해야 최신 결과를 표시합니다.

기본 설정은 예제 폴더와 현재 호스트를 수집합니다. 실제 코드 폴더를 설정의 roots에 추가하면 실행 중이지 않은 프로젝트도 찾습니다. 개인 경로가 있는 설정은 Git에서 제외되는 `inventory.local.json`에 저장하고 `--config inventory.local.json`으로 지정하세요.

```json
{
  "roots": ["C:/work/agents", "D:/services"],
  "discover_host": true,
  "track_discovered": true,
  "output": ".inventory",
  "exclude": ["D:/services/archive"],
  "discovery_depth": 4,
  "source_depth": 12,
  "max_files": 3000,
  "max_file_bytes": 1000000,
  "event_files": [],
  "source_include": [],
  "source_exclude": ["logs/*", "**/logs/*", "*.log", "*.jsonl"]
}
```

상대경로는 설정파일 위치 기준입니다. 소스는 읽기만 하며 import하거나 실행하지 않습니다. Registry와 결과물은 `output`에 기록합니다. 가상환경, node_modules, .git 내부, .env, 이름에 secret/credential/private_key가 있는 파일과 심볼릭 링크는 제외합니다. 파일의 문자열 전체나 실행 명령 전체는 저장하지 않습니다. 다만 README 목적 발췌와 경로 등은 로컬 보고서에 남으므로 외부 공유 전 확인하세요.

## 수집 항목과 근거

| 항목 | 현재 MVP 동작 |
|---|---|
| Agent 후보 | 프로젝트 마커, 소스 파일, 실행/예약 명령의 실제 파일 경로 |
| Agent 이름·목적 | agent-card.json 선언 또는 폴더명·README 첫 본문 |
| Owner | 명시 메타데이터 또는 CODEOWNERS의 전역 소유자 후보 |
| 주요 기능 | Python 함수명과 파일·행 위치 |
| Skill | SKILL.md의 이름·등록 파일 위치 |
| MCP | JSON mcpServers/mcp_servers 키 및 TOML mcp_servers 섹션 |
| API | 코드·설정의 URL 호스트만 기록. 사용자정보·경로·쿼리는 제외 |
| RAG/Data | import·의존성에 나타난 Vector DB/DB 라이브러리 후보 |
| 실행범위 | 조회/쓰기/생성 함수 패턴의 후보와 행 위치 |
| Version/배포 | Git HEAD, package.json 버전, CI·Docker 설정 파일 위치 |
| 운영상태 | 현재 프로세스와 예약 작업 정의를 별도 스냅샷에 기록 |
| 사용량 | 표준 JSONL 실행 이벤트 집계 |
| 효과 | 입력된 KPI 정의·실측 집계값으로 명시적 계산 |

코드가 존재하는 것과 실제 호출/운영 성공은 별도입니다. 의존성·패턴 검출은 `candidate`로 표시합니다. 프로세스 부재만으로 폐기/중단을 단정하지 않습니다. 이번 스캔에서 사라진 Card는 이력을 보존하고 미확인으로 표시합니다.

## PC와 서버에서 소스 찾기

Windows는 `Get-CimInstance Win32_Process`, `Get-ScheduledTask`로 읽습니다. psutil로 접근 가능한 프로세스의 작업 디렉터리를 얻어 `python main.py` 같은 상대경로를 해석합니다. Python/Node/PowerShell/셸/배치 소스 경로와 최대 3단계 wrapper의 `cd`, 공백이 있는 따옴표 경로, 배치의 `%~dp0`를 지원합니다. 작업 디렉터리 안의 `python -m package`도 파일 존재 여부만으로 해석합니다.

한 번 발견한 프로젝트 경로는 Registry에서 다음 스캔으로 이어집니다. 프로세스가 종료되거나 예약 작업이 없어져도 소스 변경을 계속 추적합니다. 중지하려는 경로는 exclude에 지정하거나 전체 지속 추적을 `track_discovered: false`로 끌 수 있습니다.

Linux는 `/proc`, 현재 사용자의 crontab, `/etc/crontab`, `/etc/cron.d`, 시스템·사용자 systemd service 정의를 읽습니다. systemd 정의 존재는 활성 상태로 간주하지 않습니다. 다른 사용자 작업과 보호된 경로는 실행 계정 권한 범위에 따릅니다.

작업 디렉터리 밖에 설치된 모듈, 동적 변수 조합, 원격 컨테이너 내부, 바이너리만 있는 서비스, 모든 셸 문법은 아직 해석하지 않습니다. 이 경우 roots에 소스 폴더를 추가합니다. 미해석 프로세스·예약 항목 수와 조회 실패는 수집 진단에 표시합니다. 서버마다 이 폴더를 복사해 실행하며 SSH 접속이나 중앙 동기화는 현재 범위에 없습니다.

## 수정 감지·승인·등록

소스 파일 내용 SHA-256을 비교하므로 생성·수정·삭제를 반영합니다. 스캔 사이에 생성됐다 사라지는 파일은 잡지 못하는 폴링 방식입니다. 같은 소스의 반복 스캔은 새 revision을 만들지 않습니다. PID와 수집시각은 소스 revision에서 제외합니다.

JSON은 agent-card.json/package.json/mcp.json/settings.json/config.json/tools.json 같은 알려진 설정 이름과 `.config.json`/`.settings.json`/`.schema.json`만 기본 포함합니다. 실행 상태나 결과 JSON이 바뀌어 승인본을 무효화하지 않도록 했습니다. 다른 구성 JSON은 `source_include: ["my-tools.json"]`으로 추가하세요. source_exclude가 include보다 우선하며 .env·비밀파일 제외 규칙도 유지합니다. 수집 깊이·파일 수·크기 제한에 걸리면 Card에 경고를 남깁니다.

```powershell
python -m agent_inventory list
python -m agent_inventory approve CARD_ID --revision REVISION --reviewer "담당자명"
```

승인은 검토한 revision에만 적용됩니다. 코드 수정 후 현재 Card는 draft가 되고 이전 승인본은 `published.json`에 유지됩니다. 오래된 revision으로 승인하면 거절합니다. 담당자 수정은 프로젝트의 `agent-card.json`에 반영한 후 재스캔합니다.

```json
{"id":"org/team/support-agent","name":"고객지원 Agent","purpose":"승인된 문서로 답변 초안 작성","owner":"CX팀"}
```

`id`가 없으면 절대경로를 기반으로 식별합니다. 경로 이동 후에도 같은 Agent로 관리하려면 명시 ID를 사용하세요. MVP는 프로젝트 디렉터리 하나를 Card 하나로 취급합니다. 한 Repo의 여러 Agent를 분리하려면 각 Agent 디렉터리를 roots로 지정하고 메타데이터를 둡니다.

승인은 로컬 CLI의 자체 선언 방식이며 SSO·역할 권한·서명 인증은 아직 없습니다. 소스 분석 누락 경고가 있는 Card는 승인할 수 없습니다.

## 스케줄러 실행

Windows에서 일회 실행:

```powershell
python -m agent_inventory.scheduled --config inventory.config.json
```

로그인 직후와 이후 10분마다 실행하도록 설치할 때:

```powershell
& .\scripts\register-task.ps1 -Python .\.venv\Scripts\python.exe -IntervalMinutes 10 -StartNow
```

개인 설정은 `-Config inventory.local.json`을 추가합니다. 현재 사용자 로그인 중 제한된 권한으로 숨김 실행하며, 로그아웃 상태에서는 새 실행을 시작하지 않습니다. 예약 작업은 가상환경의 pythonw.exe로 수집기를 직접 실행하므로 실행 시 PowerShell 스크립트 정책에 의존하지 않습니다. PC 절전·전원 종료 중에는 수집하지 않고 사용 가능한 다음 실행에서 다시 확인합니다. 예약 작업의 IgnoreNew와 파일 잠금으로 겹친 스캔을 방지합니다. 동일 작업명이 있으면 기본적으로 실패하며, 이 설치기가 만든 현재 사용자 작업만 `-UpdateExisting`으로 갱신할 수 있습니다.

상태 확인:

```powershell
Get-ScheduledTask -TaskName "Agent Inventory MVP" | Get-ScheduledTaskInfo
Get-Content .inventory/last-scan.json
```

최근 실행 로그는 `.inventory/scheduled-scan.log`에 있습니다. 종료코드 0은 정상 완료/중복 실행 생략, 2는 일부 수집 누락 또는 수집기 실패, 1은 그 밖의 CLI 오류입니다. Card 경고가 있으면 결과는 생성하되 partial로 기록합니다. 해제:

```powershell
Unregister-ScheduledTask -TaskName "Agent Inventory MVP" -Confirm:$false
```

Linux에서는 `scripts/cron.example`의 절대경로를 실제 배포경로로 바꾼 후 `crontab -e`에 추가합니다. 지속 모니터링은 서버 프로세스 관리자에서 `python3 -m agent_inventory watch --interval 30`을 실행할 수도 있습니다. 이미 있는 스케줄러의 소스를 검출하는 기능과 수집기 자체를 주기 실행하는 기능 모두 제공합니다.

## 사용량 이벤트

운영 Log가 없는 프로그램의 실제 실행 횟수·사용자 수를 프로세스 스냅샷에서 추정하지 않습니다. 아래 형식의 JSONL을 `event_files`에 등록하면 매 스캔 때 자동 집계합니다. 또는 `ingest`로 명시적으로 가져옵니다.

```json
{"event_id":"unique-run-001","agent_id":"CARD_ID","timestamp":"2026-10-21T10:00:00Z","status":"success","duration_ms":1350,"user_hash":"pseudonymous-user","task":"support","skills":["search"],"tools":["mcp:knowledge","mcp:knowledge"]}
```

```powershell
python -m agent_inventory ingest C:/logs/agent-events.jsonl
```

`event_id`는 전체 Registry에서 유일해야 합니다. 동일 이벤트는 중복 집계하지 않고, 같은 ID에 다른 값이 오면 오류로 보고합니다. 실패 상태는 `failure`입니다. tools/skills 배열은 실제 호출마다 한 항목씩 기록합니다. 집계 범위는 가져온 전체 이벤트이며 기간 필터·활성사용자 기준·로그 로테이션 최적화는 후속 범위입니다. 현재는 매번 파일 전체를 읽습니다. 원본 사용자 식별자 대신 가명화된 식별자를 전달하세요.

## KPI

```powershell
python -m agent_inventory kpi examples/kpi.json
```

평균 60분 → 40분 예제는 `33.3333%`를 계산합니다. 기준 기간·비교 기간·데이터 출처·동일 업무군·승인자·개선 방향이 없거나 값이 유효하지 않으면 계산을 거절합니다. 입력값과 비교 조건은 사람이 준비한 선언이며 실제 DB에서 가져오거나 비교군 타당성을 검증하는 기능은 없습니다. KPI 결과는 CLI JSON으로 출력되며 Card의 자동 성과 갱신 연결은 후속 범위입니다. 예제 값은 실제 PC의 성과가 아닙니다.

## 저장 구조

- `.inventory/registry.sqlite3`: 현재 Card, 전체 revision, 승인 이력, 실행 이벤트, 스캔 스냅샷
- `.inventory/inventory.json`: 전체 초안·승인 상태·실행 상태·사용량·수집 진단
- `.inventory/published.json`: 최신 승인 revision의 구성정보
- `.inventory/index.html`: 로컬 검색·상세 조회 화면
- `.inventory/last-scan.json`: 최근 수집 상태·종료코드·구조화된 진단
- `.inventory/scheduled-scan.log`: 최근 Windows 예약 실행 출력

스캔 스냅샷은 최근 500개를 보관하며, Card revision·승인 이력은 유지합니다. `.inventory`, 개인 설정, 가상환경, ZIP은 Git에서 제외합니다.

## 검증 및 범위

```powershell
python -m unittest discover -s tests -v
python -m reviews.verify_gaps
```

검증 항목은 소스 미실행, 출처 추출, 생성·수정·삭제, 재스캔 멱등성, 승인본 보존, 오래된 승인 거절, 예약 wrapper 경로, 비밀값 미저장, HTML escaping, 이벤트 중복·충돌, KPI 조건입니다. 지속 추적·상대경로·실패 상태·운영 JSON 분리·깊이 제한·중복 실행 회귀 테스트를 포함합니다. verify_gaps의 모든 값은 false여야 합니다. GitHub Actions는 Windows/Linux에서 단위 테스트를 실행합니다.

Python 3.7 이상 문법으로 구현했습니다. 분석 대상 Python 문법이 실행 Python보다 새로우면 AST 경고가 발생할 수 있으므로 서버에서는 분석 대상과 같거나 더 최신 Python을 사용하세요. 기본 터미널의 테스트 환경은 Python 3.7.4이며, 권한 확장 실행에서는 호스트 PATH의 Python이 사용되었습니다.

Windows 프로세스·예약 작업 조회를 실환경에서 검증했습니다. Linux의 실제 운영 호스트 전체 수집은 별도 검증이 필요합니다. 설치 스크립트 실행이 있어야 해당 PC의 자동 실행이 활성화됩니다. 소스 미수집 경고는 각 Card와 cards_with_warnings에, 수집기 실패는 issues의 code/severity/collector에 기록합니다.

현재 MVP의 분석은 규칙 기반입니다. LLM 목적/기능 요약, RAG 인덱스 내부·문서 연결 관계, 실제 배포 성공 조회, Test/Eval 결과 연동, 중앙 서버 동기화와 인증은 다음 단계입니다. 원본 코드를 외부 LLM에 보내는 기능은 기본 동작에 없습니다.

## 참고한 GitHub 프로젝트

기존 코드를 복사하거나 해당 제품 전체를 설치하지 않고 수집·카탈로그 구조를 참고하여 작성했습니다.

- [Backstage catalog descriptor](https://github.com/backstage/backstage/blob/master/docs/features/software-catalog/descriptor-format.md): 명시 메타데이터와 소유자·카탈로그 구조
- [psutil](https://github.com/giampaolo/psutil): Windows 프로세스 작업 디렉터리 수집에 사용
- [watchdog](https://github.com/gorakhargosh/watchdog): 파일 변경 관찰 방식. 현재 기본 구현은 의존성 없는 해시 폴링
- [cdxgen](https://github.com/cdxgen/cdxgen): 소스·의존성 기반 구성정보 수집. 현재 MVP는 제한된 Python/Node 설정만 지원
