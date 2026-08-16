# Changelog

All notable changes to this project are documented in this file.

## Unreleased

## [1.5.0] — 2026-08-17

### Added

- 캐릭터 표시를 단일 이미지에서 상태 기반 sprite/reaction 시스템으로 확장했다.
  기본 캐릭터·reaction asset pack을 번들하고, 설정 GUI에서 캐릭터 소스와 상태별
  sprite를 편집하며 저장 즉시 overlay에 반영할 수 있다. 모니터 이동·크기 변경 뒤에도
  표시 비율과 위치를 안정적으로 유지한다.
- 복합 개발 작업을 planner/coder/servant 계층으로 분리하고 독립 검수 gate를 적용하는
  orchestrate workflow를 추가했다.
- overlay의 대화형 CLI 공급자에 Codex를 추가했다. 설정 GUI와 tray/character 메뉴에서
  `codex`를 선택할 수 있고, installer가 CLI를 감지해 `~/.engram/engram-codex.cmd` shim,
  dispatcher 분기, 기존 사용자 항목을 보존하는 Engram MCP 등록을 구성한다.
- directive preflight에 deterministic Git guard 실행을 추가했다. `protected-branch`와
  `dirty-worktree`를 structured action/chore/task context로 평가하고, MCP preflight와
  audit에 guard evidence/final status를 함께 기록한다.
- local policy preflight CLI/backend role과 Claude Code·Codex `PreToolUse` 관리 hook을 추가했다.
  repo-write 범위의 `Write`/`Edit`/`MultiEdit`/`NotebookEdit`/`apply_patch` 및 명시적 Git 작업을
  preflight로 평가한다. 설정 GUI의 `directives.policy.guidance_level`에서 `off`, `warn`,
  `enforce_agents`를 선택할 수 있다. `enforce_agents`는 Claude·Codex의 유효한 정책 위반
  tool call만 차단하고 사람의 Git commit은 계속 경고 후 허용하며, backend 오류는 fail-open한다.
  overlay 시작·설정 저장·installer uninstall 시 Engram 관리 hook만 멱등 동기화한다.
  Codex user hook은 설치·변경 뒤 `/hooks`에서 사용자가 신뢰 승인해야 한다.
- provider와 무관하게 `git commit`을 검사하는 repo-local managed `pre-commit`
  hook을 추가했다. 세션 bootstrap이 Git 저장소를 감지하면 멱등 설치하고,
  `git-hook install|status|uninstall --repo <path>`로도 명시적으로 관리할 수 있다.
  uninstall은 공용 Git 디렉터리에 opt-out marker를 남겨 다음 세션의 자동 재설치를 막으며,
  명시적 install은 marker를 제거하고 다시 활성화한다.
  관리 중에는 repo-local `merge.ff=false`를 적용하고 OFF/uninstall 시 이전 값을 복원한다.
  Agent의 `git merge`는 `--no-ff`를 명시해야 하며 `--ff-only`는 정책 위반으로 분류한다.
  기존 사용자 hook이나 custom `core.hooksPath`는 덮어쓰지 않는다. 보호 브랜치의
  commit도 통과시키되 위험과 권장 branch/worktree를 안내하고 policy audit에 기록한다.
  hook launcher는 PowerShell 의존성을 제거해 Git Bash·WSL·Linux에서 동작하며, backend
  부재·실행 실패·경로 오류를 포함한 모든 managed wrapper 실패를 경고 후 허용한다.

### Fixed

- frozen 빌드에 Tcl/Tk runtime을 명시적으로 포함하고 초기화 경로를 고정해 새 설치에서
  설정창과 overlay UI가 시작되지 않는 회귀를 막았다.
- dashboard sidecar가 남긴 파일 잠금을 해제한 뒤 overlay artifact를 교체하도록 빌드
  순서를 수정하고, smoke test가 실제 사용자 DB 대신 격리 DB를 사용하게 했다.
- legacy `guidance_enabled`와 `claude_pretool_enforcement`를 config layer merge 전에
  canonical `guidance_level`로 투영해 업그레이드 후 사용자의 OFF 선택이 뒤집히지 않게 했다.
- 정책 가이드 OFF marker를 추가해 설치된 repo advisor가 backend 프로세스도 시작하지 않게 했다.
- hook 동기화 실패를 숨기지 않고 설정 GUI에 partial failure로 표시한다.
- frozen installer 재설치 화면이 기존 `user.config.yaml`의 DB/Wiki 및 작업
  디렉토리를 초기값으로 표시하고, 사용자가 확정한 두 경로만 기존 사용자 설정에
  병합하도록 수정했다. 기존 DB와 Wiki 데이터는 이동하거나 변경하지 않는다.

## [1.4.0] — 2026-08-14

### Changed

- STM/LTM 요약, working memory 체크포인트, 반성 판정, 능동 발화 프레이징에
  사용하는 격리 Claude 단발 호출이 Claude Code 기본 시스템 프롬프트 대신 최소
  전용 시스템 프롬프트를 사용하도록 변경했다. CLAUDE.md·skills·tools 차단은
  유지하면서 호출당 기본 입력 오버헤드를 약 3,000토큰에서 약 470토큰으로 줄였다.
- 외부 인간용 daily note 폴더는 더 이상 특정 사용자 경로를 기본값으로 사용하지
  않는다. 설정창에서 경로를 선택한 경우에만 Engram Wiki daily note와 함께 추가
  기록한다.

### Fixed

- KG sync의 `prune_missing`, `get_path_mtimes`, 전체 `resolve_links`를 `vault_path`
  기준으로 제한해 한 볼트의 동기화가 다른 볼트의 노드와 링크를 삭제하거나 건너뛰지
  않도록 수정했다. 볼트 경로를 정규화하고 단일 볼트의 기존 빈 경로는 자동 보정한다.

## [1.3.0] — 2026-08-14

### Added

- Overlay 빌드 경로를 `installer/build-overlay.ps1` 단일 엔진으로 통합했다.
  임시 dist, build manifest, PyInstaller 실패 시 clean 재시도, 역할별 smoke-check를 사용하며
  성공한 결과만 dist/deploy 대상에 교체한다. `build-installer.ps1 -SkipBuild`는
  검증되지 않은 오래된 번들을 거부한다.
- Streamlit dashboard를 외부 Python 대신 전용 `engram-dashboard.exe` sidecar로
  번들한다. overlay가 설정에 따라 sidecar를 시작·종료하며, 컨텍스트 설정의
  `대시보드 자동 실행`과 `대시보드 보기`로 동작을 제어한다. 빌드 시 AppTest 렌더와
  임시 포트 health smoke를 모두 통과해야 배포된다.
- 오프라인 임베딩 모델에 `resource/embedding-model/manifest.json`과 파일별 SHA-256을
  추가했다. 기존에 일치하는 모델은 재다운로드·재export하지 않으며, MCP FastMCP 1.x
  import 호환성도 설치·번들 smoke 단계에서 확인한다.
- 시맨틱 임베딩을 `intfloat/multilingual-e5-small`로 전환하고 저장 문서에는 `passage:`,
  검색 질의에는 `query:` 역할 prefix를 적용한다. KGNode/EpisodeNode에 모델 스탬프를
  저장해 같은 384차원의 구형 벡터도 감지하며, mismatch 벡터는 검색에서 즉시 제외하고
  다음 KG sync에서 SQLite 원본으로 전체 재임베딩한다. frozen installer도 모델 manifest와
  파일 hash를 검증하고 모델 ID·384차원을 확인한다.
- 열린 세션이 30분간 유휴 상태이고 마지막 체크포인트 이후 사용자 발화가 5회 이상이면
  세션을 닫지 않고 자동 메모리 체크포인트를 생성한다. working memory는 항상 갱신하고
  novelty gate 통과 시에만 LTM으로 승격하며, activity log·Engram `docs/daily`·
  `~/vault623/daily_notes`·연관 프로젝트 Progress를 같은 ID로 상호 연결한다.
- Wiki 작성·수정과 세션 종료·반성을 각각 `engram-wiki-workflow`,
  `engram-close-session` skill로 승급했다. source/frozen installer 모두 Copilot과
  Claude Code 사용자 skill 경로에 설치한다.
- 저장소 변경 작업의 Wiki 선행 기록 확인, 보호 브랜치 이탈, 검증, activity 기록을
  `engram-task-workflow` skill로 통합했다. 핵심 workflow dispatcher는 directive 항목
  상한과 무관하게 항상 컨텍스트에 남도록 명시적으로 고정한다.
- 다중 에이전트 병렬 작업, 기존 dirty worktree와 다른 성격의 작업, 장시간 실험은
  작업별 branch+worktree로 격리한다. installer가 협업 가이드도 신규 Wiki에 배포한다.
- directive policy 엔진 기반을 추가했다. directives 스키마에 structured policy
  metadata·workflow/guard 식별자·legacy migration marker를 보존하고, installer seed를
  workflow/advisory 규칙으로 승격했다. deterministic preflight evaluator와 audit
  조회 MCP 도구는 실제 hook 실행 없이 allow/workflow_required/blocked 요구사항만
  보고한다.

### Fixed

- frozen installer가 Copilot의 `/engram` 및 `engram-new-session` skill을 번들·사용자
  skill 경로에 배포하지 않던 누락을 수정했다. 설치 직후 이미 실행 중인 CLI는
  환경변수와 skill registry를 갱신하지 못하므로 새 터미널/세션이 필요하다는 안내도
  추가했다.
- source installer가 Ollama를 사용하지 않는 provider에서도 `overlay.yaml` 예시 모델과
  폐기된 STM 요약 모델을 확인·다운로드하던 문제를 제거했다. Ollama 모델 처리는
  `ollama`/`claude-code-ollama`를 명시적으로 선택했을 때만 수행한다.
- 조건부 directive가 세션 시작의 빈 query 때문에 주입되지 않던 구조를 제거했다.
  기본 directive를 항상 보이는 짧은 정책·workflow dispatch로 정리하고, 중복 Wiki 및
  reflection directive를 제거했다. installer 관리 기본값은 기존 DB에도 마이그레이션하되
  사용자 수정 directive는 보존한다.
- **v1.2.1 portable exe hotfix 2** — 기본 채팅 모드인 bubble에서도
  `session.auto_inject` 설정과 무관하게 `engram_get_context_once` 부트스트랩 지시문을
  항상 주입한다. 신규 설치 직후에도 첫 응답 전에 정체성·튜토리얼 컨텍스트를 로드한다.
- **v1.2.1 portable exe hotfix** — frozen 설치본에서 `~/.engram/engram-overlay.cmd`가
  없어도 현재 `engram-overlay.exe`를 자동 시작 바로가기 대상으로 사용한다. installer가
  이미 만든 바로가기는 설정 저장 시 불필요하게 다시 만들지 않는다.
- 입력·에코 말풍선 꼬리가 캐릭터가 아닌 해당 모니터 하단 중앙을 향하도록 조정해
  사용자 발화와 캐릭터 응답의 화자 방향을 구분한다.

## [1.2.1] — 2026-08-13

### Fixed

- frozen MCP 첫 실행이 Windows 파일 스캔으로 늦어질 때 health monitor가 준비 중인
  프로세스를 반복 종료하던 문제를 수정했다. 실제 감독 코드가 읽는
  `config/overlay.yaml`에 시작 유예·실패 임계값을 배치하고, installer가 런타임
  config를 exe 옆에 외부 파일로 배포해 Python exe 재빌드 없이도 설정을 교정할 수
  있게 했다.

## [1.2.0] — 2026-08-13

### Added

- Episode 검색 결과에서 `EP_TO_KG`와 `KG_EDGE`를 최대 2홉까지 순회하는
  graph-aware retrieval API를 추가했다. Episode 관련도, 링크 신뢰도, KG 엣지
  가중치, 홉 감쇠를 융합해 점수를 계산하고 최상 근거 경로를 함께 반환한다.
- 필터를 통과한 Episode에서 도달한 KG 노드와 최상 경로를
  `<ctx:graph_evidence>`로 컨텍스트에 주입한다. 근거가 없는 질의에는 섹션을
  생성하지 않고 경로·요약 길이를 설정값으로 제한한다.
- direct·패러프레이즈·negative-control golden query와 실제 임시 Kuzu 기반 E2E를
  추가했다. `scripts/kg/evaluate_graph_retrieval.py`로 운영 DB의 Episode/KG hit를
  재평가하고 JSON 결과 및 실패 exit code를 받을 수 있다.
- SQLite `memories`를 기준으로 KuzuDB `EpisodeNode` 무결성을 점검하는 관리자 reconcile
  도구와 CLI를 추가했다. 기본은 dry-run이며 명시적인 apply에서만 stale 노드와 연결을
  삭제하고, 누락 및 미연결 ID는 생성하지 않고 보고한다.
- `EP_TO_KG`에 점수, 방식, 모델, 버전, 생성 시각 등 링크 provenance 메타데이터를
  추가하고 기존 KuzuDB 스키마를 자동 마이그레이션한다.
- 세션 후 동기화의 실행·단계·오류·드리프트를 원자적 JSON 상태로 보존하고
  `/health` 요약 및 로컬 `/api/sync/status`에서 조회할 수 있게 했다.

### Fixed

- graph retrieval 운영 평가의 direct golden query를 실제 2홉 제한 결정에 맞추고,
  최신 Episode가 연결된 메모리 설계 노드도 유효 anchor로 인정하도록 교정했다.
- raw Copilot CLI 새 세션에서도 `~/.engram/copilot-instructions.md`를 읽도록
  `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`를 사용자 환경변수로 등록한다. frozen installer도
  프로토콜 파일을 배포하고, `engram-copilot`은 `--yolo`·`--model` 같은 대화형
  옵션이 있어도 bootstrap initial prompt를 유지한다.
- JSON 태그의 구두점과 제목 전용 KG 노드도 일관되게 정규화하여 메모리 키워드 링크가
  생성되지 않던 문제를 수정했다.
- 키워드 하나만 겹쳐도 모든 KG 노드에 연결되던 과잉 링크를 막았다. 불용어·숫자를
  제외하고 최소 2개 일치, 정규화 점수 순 Episode당 최대 3개로 제한한다.
- MCP 이벤트 루프와 백그라운드 동기화 스레드가 동일한 `asyncio.Lock`을 공유해
  교착될 수 있던 문제를 cross-loop 안전 락으로 교체했다.
- SQLite/Kuzu Episode 대조 결과에 따라 뒤처지거나 앞선 memory 체크포인트를 자동
  보정하고, upsert 실패 행 이후로 체크포인트가 진행되지 않게 했다.
- `engram_close_session`이 동기화 예약 결과를 별도로 반환해 예약과 실제 완료를
  구분하며, 실패한 동기화에는 cooldown을 적용하지 않아 다음 세션에서 재시도한다.
- sync gate/cooldown skip이 마지막 성공·실패 결과를 덮어쓰지 않게 예약 이력을
  분리하고, 성공 후 drift를 다시 측정해 최종 checkpoint와 Episode count를 확정한다.
- 프로세스 재시작 시 남은 `scheduled`/`running` 상태를 중단 실패로 복구하고,
  동시 `close_session` 예약은 원자적 run guard로 하나만 실행되게 했다.
- Kuzu KG 동기화에 watchdog 취소 신호를 전달해 장시간 노드 동기화를 안전하게
  중단할 수 있게 했다.
- `close_session`이 생성한 memory/Episode에 실제 session ID를 보존해 원본
  user/assistant 턴으로 역추적할 수 있게 했다.
- 자동 장기기억 검색에서 짧은 테스트 Episode를 제외하고 현재 프로젝트 일치도를
  반영해 재순위화하며, 낮은 관련도의 타 프로젝트 기억은 컨텍스트에서 억제한다.
- Episode 검색 후보를 자르기 전에 프로젝트·테스트 필터를 적용하고, portable project
  anchor를 사용해 경로가 달라져도 같은 프로젝트 KG 범위로 연결한다.
- `EP_TO_KG` 후보를 프로젝트 경로·노드 타입으로 제한하고, 기존 링크 교체를 단일
  transaction으로 처리해 후보 계산이나 생성 실패 시 이전 링크를 보존한다.
- 운영 `memories_sync` endpoint와 배치 CLI가 하드코딩된 임계값 대신
  `memory.ep_to_kg.semantic_threshold` 설정을 사용하게 했다.
- 수동 `memories_sync` 성공 후 checkpoint, Episode count, drift를 persistent sync
  status에 반영해 `/health`가 실제 canonical/Kuzu 상태를 표시하게 했다.
- Episode에 명시된 `무관 질의` 평가 주제와 현재 질문이 일치하면 해당 평가용 기억을
  자동 컨텍스트에서 제외해 negative-control 문구 자체가 회상되는 오염을 막았다.
- 프로젝트 키의 경로 digest를 제외해 동일 프로젝트를 안정적으로 판별하고, 명시적
  타 프로젝트 Episode 및 한국어 질의에서 실제 토큰 근거가 없는 후보를 제외한다.
- 괄호·인용부호 뒤에서 분리된 한국어 조사 한 글자가 관련성 근거로 계산되지 않도록
  semantic 재순위화와 SQLite fallback의 검색 토큰 정규화를 통일했다.
- 한국어 조사·서술 접미사를 제거해 패러프레이즈의 공통 어근을 인식하고, 최소 두
  근거 토큰을 요구해 `환경` 같은 일반 단어 하나만 겹친 고점 오탐을 차단한다.

## [1.1.2] — 2026-08-11

### Changed

- **말풍선 대화 모드를 기본값으로 전환** — 신규 설치 및 사용자 오버라이드가 없는
  환경은 이제 `overlay.chat_mode: bubble`로 시작한다. 설정에서 TUI 모드를 명시적으로
  선택하면 `overlay.user.yaml`에 `tui`를 저장해 기본값 변경 이후에도 선택을 유지한다.
## [1.1.1] — 2026-08-07

말풍선 두 건. 하나는 진짜 버그였고, 하나는 우리 버그가 아니었다.

### Fixed

- **응답 말풍선이 최대 높이를 넘겨도 스크롤바가 안 나오던 문제** — 내용은 잘리는데
  잘린 뒤를 볼 방법이 없었다. 스크롤 수단 **두 개를 양쪽 다 막아놓고** 높이만
  자르고 있었다:
  `needs_scroll` 조건의 `not used_html` 가드가 HTML 경로에서 캔버스 스크롤바를
  아예 만들지 않았고(`body_h`는 그대로 `max_body_h`로 잘랐다), `HtmlFrame`도
  `vertical_scrollbar=False`로 생성해 tkinterweb 자체 스크롤바까지 꺼져 있었다.

  tk.Text 폴백 경로엔 그 가드가 없어서, **tkinterweb 로드가 실패한 머신에서만
  "제대로 되는" 것처럼 보였다** — 잘 되는 쪽이 예비 경로였던 셈이고 양쪽 다 같은
  버그를 갖고 있었다.

  `vertical_scrollbar="auto"`로 켜고, `place()`로 최종 높이가 확정된 뒤 표시 여부를
  강제로 다시 계산한다. tkinterweb의 `AutoScrollbar`는 tkhtml이 `yscrollcommand`를
  쏠 때만 갱신되는데 **높이가 줄어드는 시점엔 그 콜백이 안 튀고**, `after_idle`조차
  tkhtml 리플로우보다 먼저 도는 것을 실측으로 확인해 짧은 지연을 하나 더 뒀다.

### Added

- **생각 풍선 표시 방식 옵션** (`bubble.thought_detail` = `full` | `brief`,
  설정 창 "말풍선" 탭). 기본값은 `full`(기존 동작).

  발단은 "이 PC만 생각 풍선이 옛날 버전처럼 나온다"였는데, 파보니 **우리 쪽 버그가
  아니었다.** 최신 Claude Code CLI(2.1.223)는 `thinking_delta` 이벤트는 보내되
  `thinking` 텍스트를 비워서 보내고 `estimated_tokens`만 준다 — 실측으로 확인했다
  (추론 강제 프롬프트에서 `thinking_delta` 4건 / 실제 텍스트 0자 /
  `estimated_tokens: 100`). 그 100이 그대로 "생각을 정리하는 중…" 문구가 된다.
  **오버레이가 만들어낼 수 없는 내용이라 `full`로도 복구되지 않는다.**

  차이는 버전에서 갈렸다. 이 머신은 native 단독 바이너리라 스스로 최신으로
  갱신되고, npm 설치본은 사용자가 올려야 갱신된다 — 그래서 한쪽만 먼저 새 동작을
  받았다. 즉 **뒤처진 게 아니라 앞서 있던 것**이다.

  그래서 이 옵션의 실용적 방향은 반대다. `brief`는 CLI가 내용을 주는 환경에서도
  항상 짧은 문구로 고정해, 머신마다 다르게 보이는 편차를 없앤다. 표시할 때만
  축약하므로(원문은 보존) 설정 저장 즉시 반영되고 재시작이 필요 없다.

## [1.1.0] — 2026-08-06

임베딩을 타는 MCP 호출이 서버 전체를 멈춰 세우던 문제를 고쳤다. 곁들여 위키를
고칠 수 있는 범위가 넓어졌고, 페르소나가 프로젝트를 넘어 따라오게 됐다.

### Fixed

- **임베딩 호출 120초 행 → transport 드롭** (#5). 증상은 "가끔 느리다"가 아니라
  "특정 호출만 매번 죽는다"였다. 원인은 두 겹이었다.

  첫째, FastMCP 는 동기(`def`) 툴 함수를 스레드풀로 넘기지 않고 이벤트 루프에서
  그대로 호출한다(`func_metadata.py:93-96`). `kg_semantic_search`·`kg_add_note`·
  `kg_update_node`·`kg_patch_section`, 그리고 거의 모든 세션이 시작할 때 부르는
  `engram_get_context` 까지 전부 `def` 였다. **한 호출이 오래 걸리면 서버 전체가
  선다** — 그 순간 붙어 있는 다른 클라이언트까지 같이.

  둘째, `SemanticGraph` 가 `kuzu.Connection` **하나**를 공유하고 있었다. KuzuDB
  자신은 동시성이 필요하면 `AsyncConnection` 으로 커넥션 **풀**을 만든다 —
  단일 커넥션 공유는 애초에 지원 대상이 아니다.

  둘 다 걷어냈다. `AsyncConnection` 으로 바꾸고 호출 경로 전체를 async 로 올렸다.
  `threading.RLock` → `asyncio.Lock` 전환 과정에서 재진입이 불가능해지므로,
  락을 쥔 채 다시 락을 잡던 자리는 `_locked` 내부 메서드로 분리했다.

  진짜 범인은 KuzuDB 가 아니라 임베딩이었을 가능성이 크다. `compute_embedding`
  /`_get_encoder` 는 KuzuDB 를 건드리지 않는다 — `SentenceTransformer` 로드와
  `encode()` 가 동기로 루프를 잡는다(로컬 로드 실패 시 조용히 Hub 다운로드로
  폴백하는 경로까지 있다). `asyncio.to_thread` 로 떼어냈다. 이걸 빼면
  `AsyncConnection` 전환은 서류상으로만 끝나고 행은 그대로 재현된다.

  읽기 경로(`semantic_search` 등)는 원래 락이 아예 없었다. 지금까지 안 터진 건
  FastMCP 가 우연히 모든 호출을 한 스레드로 직렬화해준 덕이었다 — 진짜 병렬이
  되는 순간 캐시 인덱스가 어긋난 검색 결과가 조용히 나올 수 있는 상태였다.
  읽기도 같은 락으로 덮었다. 동시성 테스트 9개를 새로 붙였다.

- **터널 목록에서 제거해도 되살아나던 문제** — `stop()` 만으로는 `STATE_DOWN`
  엔트리가 딕셔너리에 남아, 주기 갱신이 "살아 있는 고아 터널"로 오인해 목록에
  다시 넣었다. `remove()` 로 내부 상태까지 지운다.

- **페르소나가 다른 프로젝트에서 무시되던 문제** — 지침 원문이
  "항상 **engram 페르소나**(므네마)의 …" 였다. 별도 CLAUDE.md 가 있는 프로젝트
  세션에서 모델은 이걸 "engram 프로젝트 전용 정체성"으로 읽고 **명시적으로
  거부했다**. 지침을 어긴 게 아니라 쓰인 대로 읽은 것이다. 정체성 규칙에
  프로젝트명을 수식어로 넣으면 스코프 한정자가 된다. 문구를 "페르소나는
  프로젝트가 아니라 '나'에 속한다"로 다시 쓰고, 새 설치에도 반영되도록
  `directives.json` seed 에 추가했다(기존에는 seed 에 아예 없어서 런타임에
  추가된 환경에만 존재했다).

### Added

- **`kg_patch_section`** — `kg_update_node` 는 summary/Progress/open_intents
  세 슬롯만 정규식으로 갈아끼운다. 그 밖의 본문(헤더의 구 URL, 아키텍처 서술)은
  고칠 방법이 없었고, 원격 세션은 vault 파일에 직접 닿을 수도 없다(WAL SQLite 와
  임베디드 KuzuDB 때문에 파일 공유는 금지다 — 서버를 공유하지 파일을 공유하지
  않는다). 헤딩 단위로 본문을 교체하는 툴을 추가했다. `engram_close_session` 이
  함께 쓰는 기존 경로는 건드리지 않았다.

- **`kg_add_note(subdir=...)`** — `title` 에 슬래시를 넣어도 슬러그화 과정에서
  지워져 `projects/` 루트에 평평하게 떨어졌다(lint 가 규칙 위반으로 잡아냈다).
  우회로였던 `note_type="projects/my-project"` 트릭은 그 문자열이 그대로 DB
  `type` 에 박혀, 다음 `kg_sync` 에서 `NODE_TYPES` 검사에 걸려 `concept` 으로
  강등됐다. **위치와 타입이 한 파라미터에 뭉쳐 있던 게 원인**이라 분리했다.
  경로 탈출 가드 포함.

- **캐릭터 좌우 반전** — 설정창 체크박스와 우클릭 메뉴 양쪽. 우클릭 토글은 즉시
  다시 그린다.

## [1.0.0] — 2026-07-31

원격에서 engram 을 쓸 수 있게 됐다. 기억과 위키는 여전히 로컬 한 곳에만 있고,
원격 세션이 SSH 리버스 터널로 그 하나를 공유한다.

### Added

- **원격 접근용 인증 리스너 분리** — engram MCP 의 보안 모델은 통째로
  "loopback 바인딩 = 인증"이었다. 인증 코드가 없는 게 아니라 필요가 없었다.
  `127.0.0.1:17385` 에 닿을 수 있는 프로세스는 이미 로컬 실행 권한이 있으므로
  인증을 걸어도 새로 막히는 게 없다. SSH 리버스 터널은 그 등식을 깬다 —
  터널 너머 머신은 로컬 권한이 없는데 포트에는 닿는다. **도달 ≠ 권한.**

  그래서 리스너를 둘로 나눴다. 같은 프로세스에서 소켓 둘을 바인딩하고
  (`uvicorn.Server.serve(sockets=[...])`), 미들웨어가 요청이 들어온 로컬 포트로 분기한다.

  | 포트 | 대상 | 인증 | 도구 제한 | 경로 | 감사 |
  |---|---|---|---|---|---|
  | 17385 | 로컬 | 없음(유지) | 없음 | 전체 | 없음 |
  | 17386 | 원격 | bearer | principal 별 deny | MCP 만 | 건별 기록 |

  기존 로컬 클라이언트(overlay bubble·VS Code·kg_watcher·claude-code)는 무변경이다.
  미들웨어는 `BaseHTTPMiddleware` 가 아니라 순수 ASGI 다 — 전자는 SSE 스트리밍을
  깨뜨리고 본문을 읽으면 downstream 이 굶는다.

- **토큰별 도구 deny 와 scope 고정** (`~/.engram/mcp-tokens.yaml`, fail closed).
  기본 deny 는 로컬 실행으로 이어지거나(`engram_consult_engram`), 이후 모든 세션에
  영향을 남기거나(`engram_add_directive`), 외부로 발신하거나(`engram_discord_send`),
  가드가 보안 경계가 아닌(`kg_cypher`) 도구들이다.

  `scope` 를 지정하면 `tools/call` 인자에 강제 주입된다. 원격 `cwd` 는 서버에 없는
  경로라 스코프가 조용히 global 로 폴백하는데, 그러면 연속체가 기억상실에 걸린다.
  **원격 전용 스코프를 새로 파는 건 답이 아니다** — 격리는 되지만 같은 문제를
  이름만 바꿔 반복하는 것이다. 연속성이 이미 쌓인 스코프를 그대로 쓴다.

- **원격 접근 감사 로그** — `~/.engram/logs/remote-audit.jsonl` 에 건별 즉시 append.
  기존 인메모리 링버퍼(maxlen=100, 종료 시에만 flush)는 감사용으로 쓸 수 없다.
  17386 을 지나는 요청은 정의상 전부 원격이라 origin 구분이 공짜로 해결된다.

- **설정에 "원격" 탭** — 리스너 상태, 토큰 목록(name/scope 만 — 값은 UI 에 싣지 않는다),
  터널 목록, 키 등록, 최근 감사 로그. 목록은 오버레이 재시작(재빌드 등)을 넘어
  유지되고, 연결은 [연결]을 눌러 그때 로그인한다. 설정에 없어도 **실제로 열려 있는
  터널은 항상 목록에 띄운다** — 열려 있는데 화면에 없는 상태를 만들지 않는다.

- **터널 관리자** (`overlay/remote_tunnel.py`) — 오버레이가 `ssh -N -R` 를 자식으로
  소유한다. 설계의 축은 `ExitOnForwardFailure=yes` 다. 이게 없으면 ssh 는 살아 있는데
  `-R` 바인딩만 실패한 좀비가 생겨 "프로세스 생존 ≠ 터널 생존"이 된다.

  키 인증이 되면 창 없이 붙고, 안 되면 그때만 콘솔을 띄워 비밀번호를 받는다.
  로그인이 끝나면 [창 숨기기]로 콘솔을 치운다(창은 conhost 소유라 `AttachConsole`
  로 붙어 핸들을 얻는다). 자동 재연결은 토글이며 기본 꺼짐이다.

  자식은 `KILL_ON_JOB_CLOSE` Job Object 에 묶인다. Windows 는 부모가 죽어도 자식을
  죽이지 않아, 오버레이가 크래시하면 터널이 고아로 남아 원격 포트를 점유하고
  이후 연결이 영영 실패한다.

- **원격 등록 자동화** (`scripts/setup-remote.ps1`) — 토큰 선택 → 전송 → 원격
  `~/.claude.json` 등록 → 실호출 검증까지. Linux/macOS/Windows 원격 모두 지원.
  토큰 값은 화면·로그·argv 어디에도 노출되지 않는다(ssh stdin 전용).

- **사용 매뉴얼** (`docs/remote-access.md`) — Windows/Ubuntu, SSH 터미널,
  VS Code Remote-SSH, ORCA 각 경우. ORCA 는 시스템 ssh 가 아니라 ssh2(순수 JS)를 쓰고
  `RemoteForward` 를 구현하지 않으므로 터널을 별도로 잡아야 한다.

### Fixed

- **`config/config.yaml` 이 소스·설치본 양쪽에서 무시되던 경로 리그레션** —
  `423db3a`(core 패키지 재편)에서 `runtime_config.py` 가 `core/` → `core/config/` 로
  이동했는데 루트 계산이 따라가지 않았다. 그 결과 `tools.disabled`(15개)가 통째로
  무력화돼 있었고, `engram_consult_engram`(Copilot CLI 를 `--allow-all-tools` 로
  로컬 spawn)이 노출된 상태였다. 노출 도구 57 → 42개.

- **원격 리스너의 비-MCP 경로 우회** — 원격 리스너는 로컬과 같은 ASGI 앱을 공유하는데,
  그 앱의 `/api/sg/*`·`/kg_sync`·`/memories_sync` 는 도구 계층을 거치지 않아
  토큰별 deny 가 적용되지 않았다. `/api/sg/graph` 가 토큰만으로 그래프 전체를 덤프했고
  `tools.disabled` 로 막아둔 도구도 HTTP 라우트로는 닿았다. 허용 목록으로 뒤집어
  MCP 전송 경로와 `/health` 외에는 404 로 막는다.

- **명령 주입** — 키 등록이 `cmd` 문자열을 조립하며 호스트를 그대로 삽입했다.
  호스트는 `overlay.user.yaml` 에서도 오므로 신뢰할 수 없다. ssh_config 별칭
  화이트리스트 + 문자셋 검증으로 차단하고, 선두 `-` 호스트(ssh 옵션 주입)도 막는다.

- 감사 로그·ssh stderr 는 원격이 제어하는 값이라 개행을 제거해 UI 행 위조를 막는다.
- 재빌드 시 `dist` 파일 잠금으로 실패하던 것을 재시도로 해결.

### Notes

- Windows 원격에서 키 인증이 안 되면 거의 항상 **관리자 계정** 문제다. OpenSSH 는
  관리자의 `~/.ssh/authorized_keys` 를 읽지 않고
  `C:\ProgramData\ssh\administrators_authorized_keys` 만 본다. ACL 을 좁히지 않으면
  파일을 조용히 무시한다. 자세한 절차는 `docs/remote-access.md`.
- 원격 리스너는 기본 꺼짐(`mcp.remote_enabled: false`)이다.

## [0.2.2] — 2026-07-30

### Changed

- **연속체 이름 `아로나` → `므네마`(Mnema)** — 저작권 회피. Mnema 는 그리스어 μνῆμα로
  "기억" 그리고 "남아있는 것" — engram(기억의 물리적 흔적)과 같은 개념의 다른 언어다.
  이름의 실제 출처는 DB `identity.name` 한 곳이라 코드 변경은 거의 없다.

### Added

- **자율발화 후처리(feedback) 루프** — 발화하고 끝나던 걸 끊고, 결과를 결과로 남긴다.
  참여 판정을 `engaged` / `acknowledged_no_reply`(열어보고 무응답) / `ignored`(페이드·
  "나중에"·무관한 새 턴) 3가지로 나누고, 확정 시 `InitiativeEngine` 의 `on_outcome`
  콜백이 1회 호출된다. 결과별로 `activity_log` 에 `initiative.<outcome>` 기록,
  참여한 호기심은 `address_curiosity(ref_id)` 로 자동 해소, 같은 소재 3연속 무시 시
  `initiative.low_interest` 를 남긴다. `Nudge` 에 `topic`/`ref_id` 추가 —
  발화 메타가 결과 판정까지 살아남는다. (설계: `docs/initiative-v2-design.html`)
  결과에는 **반응까지 걸린 시간(latency)** 이 함께 기록된다 — 같은 `ignored` 라도
  3초 만에 물린 것과 25초 dwell 을 다 채우고 페이드된 것은 정반대 신호(관심 없음 vs
  자리에 없었음)라 이 값 없이는 구분되지 않고, 소급 복원도 불가능하다.
  `detail` 은 첫 `" | "` 앞을 기계 판독용 `key=value` 구간(값에 공백 없는 것만)으로,
  뒤를 자유 텍스트(topic·문구)로 나눈다 — 나중에 집계기를 붙일 때 topic 에 구분자가
  섞여도 파싱이 깨지지 않게.
- **답장(REPLY) 아이콘** — 능동 발화 풍선 아래 모서리에 SNS 식 답장 화살표 배지가
  반쯤 걸쳐 붙는다. 눌러도 되는 자리가 보여야 "무시"와 "못 봤음"이 구분된다.
  누르면(= 풍선 클릭) **빈 입력창이 열린다 — 자동 전송도, 문구 자동 생성도 없다.**
  발화 문구는 세션에 보낼 때 prepend 되므로 짧게 답해도 맥락이 통한다. 안 쓰고 닫으면
  무응답으로 기록.
  **꼬리 반대쪽** 모서리에 놓아 꼬리와 겹치지 않는다. 말풍선 자기 캔버스의 도형으로
  그려서 폰트 글리프에 의존하지 않고, 위치·z순서·페이드·드래그가 전부 풍선을 따라간다.
- **지난 발화에 나중에 답하기** — dwell(25초)은 **결과를 판정하는 창**일 뿐이고 답할 수
  있는 창이 아니다. 캐릭터를 아무 때나 눌러 "마지막에 뭐라고 했더라"를 확인했을 때,
  그게 자율발화였으면 답장 버튼과 문구 prepend가 그대로 살아난다. 마지막이 평범한
  응답이면 버튼 없이 그냥 새 대화를 시작한다(`_last_was_nudge` 로 구분).
  뒤늦은 답장은 `initiative.late_engaged` 로 **별도 기록**한다 — 이미 남긴
  `ignored` 를 소급 수정하지 않아 "발화 1건 = 결과 1건" 불변식이 유지되면서도
  "결국 응했다"는 신호를 잃지 않는다. 백오프는 리셋되고, curiosity 소재였으면
  이 경로에서도 `address_curiosity` 가 걸린다.
- **발화 문구 연속성** — 대화로 이을 때 세션에 보내는 첫 프롬프트 앞에 화면에 실제로
  떠 있던 발화 문구를 얹는다(프레이징을 거쳤으면 그 문장). 캐릭터가 먼저 건넨 말을
  세션이 모른 채 답하던 문제 해결.
- **발화 보류 이유 로깅** — `_blocking_reason()` 이 막고 있는 첫 조건을 사람이 읽을
  문구로 내놓고, 상태가 **바뀔 때만** INFO 한 줄 남긴다. 이게 없어서 "자율발화가 아예
  안 뜬다"의 원인을 Win32 창 열거까지 동원해 찾아야 했다. `main._bubble_screen_clear()`
  도 넷 중 무엇이 막았는지(모드/턴/입력창/풍선) 같은 방식으로 남긴다.

### Fixed

- **무시 백오프가 사실상 작동하지 않던 문제** — 모든 사용자 입력이 무조건
  `notify_engaged()` 를 불러서, 자율발화와 무관한 평소 대화가 백오프를 계속 0 으로
  되돌렸다. 이제 자율발화에 대한 답장일 때만 참여로 친다.
- **무시가 관측된 적이 없던 문제** — 페이드 완료 시 엔진에 통지가 없었고, 대신
  `_speak()` 이 발화 시점에 `ignore_streak` 을 미리 올렸다. 이제 결과 확정 시점에
  한 번만 움직인다(`acknowledged` 는 절반 스텝).
- **폐기된 발화가 비용을 그대로 물던 문제** — 프레이징이 도는 사이 화면이 바빠져
  렌더를 건너뛴 경우에도 발화 간격·소스 쿨다운이 소비된 채 남았다. 이제 환불되어
  화면이 다시 비면 밀리지 않고 재시도한다.
- **보류 이유 로그가 45초마다 도배되던 문제** — "상태 변화 시 1회"를 메시지 문자열
  비교로 판정했는데, 문구에 경과 초가 들어가서(`발화 간격 부족 2655s < 3600s`) 매 tick
  달라져 중복 판정이 무력화됐다. `(key, message)` 로 분리해 key 로만 판정한다.

### Known issues

- `bubble.speech_fade: false` 로 두면 응답 풍선이 영구히 남아 `is_idle()` 게이트가 계속
  False 가 되어 **자율발화가 아예 뜨지 않는다.** 이 항목은 설정창에 노출돼 있지 않고,
  `overlay.user.yaml` 직접 편집은 파일 감시가 없어 오버레이 재시작이 필요하다.
  자율발화를 쓰려면 페이드를 켜고 `speech_dwell_ms` 로 유지 시간을 조절할 것 —
  캐릭터를 클릭하면 `replay_last()` 가 마지막 교환을 되살리므로 잃는 건 없다.

## [0.2.1] — 2026-07-28

### Added

- **능동 발화 (initiative)** — 말풍선 모드에서 유휴 시 캐릭터가 스스로 말을 건다.
  소재: 미완 작업(working memory `open_intents`) · 미해결 호기심 · git 미커밋/미push ·
  persona 혼잣말. 가드: 유휴 대기 · 최소 간격 · 조용한 시간대 · 소스별 쿨다운 · 무시 백오프.
  문구는 하이브리드 — 템플릿 폴백 + 격리된 1회성 LLM 프레이징(persona 말투, 상주 세션
  STM/resume 무오염). 설정창 → 오버레이 탭 "능동 발화" 그룹으로 on/off·빈도 조절
  (저장 즉시 반영). 기본 꺼짐. (`overlay/bubble/initiative.py` 신규,
  `config/overlay.yaml` 의 `bubble.initiative`)
- **클릭으로 마지막 교환 복원** — 캐릭터를 클릭해 입력창을 열 때, 페이드로 사라진 마지막
  응답(+질문 에코)을 되살린다. 자율발화(teal 단독) vs 사용자 질문(응답+에코)이 색·구성만으로
  구분됨.

### Changed

- **테마 갱신 방식 전환** — 메시지 원문에서 명사를 추출하던 방식(어절 부스러기 누적)을
  폐기하고, **세션 종료 시 Claude 판정**으로 의미 단위 관심사 라벨을 갱신한다
  (`core/graph/semantic/stm_promoter.py`). MCP `engram_update_themes(text)` →
  `engram_update_themes(themes: list[str])` 로 시그니처 변경, `record_*_message` 의
  `update_themes` 인자 제거.
- **curiosity 품질 정리** — 같은 topic 의 pending 중복 방지(`add_curiosity(dedup=True)`),
  오래 안 다뤄진 pending 자동 폐기(`expire_stale_curiosities`, 기본 14일) 및 처리된 항목
  정리(`purge_processed_curiosities`, 기본 30일). context 주입 규칙에 "실제로 다뤄서
  해소되면 `engram_address_curiosity(id)` 로 표시" 명시. 대시보드 그래프는 pending 만 표시.

## [0.2.0] — 2026-07-27

### Added

- **말풍선 최대 높이 제한 + 스크롤** — `speech_max_height_ratio`(기본 0.55) /
  `thought_max_height_ratio`(기본 0.30) 설정으로 모니터 작업영역 높이 대비 상한을 지정.
  초과 시 스크롤바 자동 표시. 설정창 → 말풍선 탭에 슬라이더 UI 추가(0 = 무제한).
- **grip 높이 조절** — 말풍선/생각풍선 코너 grip을 수직 드래그해 최대 높이를 실시간 override.
  위로 올리면 확장, 아래로 내리면 축소(재시작 전까지 유지).
- **생각풍선 스크롤** — `canvas.create_text` → `tk.Text` 위젯 전환으로 스크롤 지원.
- **전역 engram 자동 부트스트랩** (`core/integrations/engram_bootstrap.py` 신규)
  · 설정 `session.auto_inject` 켜면 `~/.claude/settings.json`에 `SessionStart` hook 자동 등록.
  · hook이 `engram_get_context_once` 호출 지시문을 세션 컨텍스트에 주입 — 오버레이 바깥의
    Claude Code 세션(데스크톱 앱 / 순정 CLI)에도 적용.
  · Bubble 세션도 `append_system_prompt`로 동일 지시문 주입(auto_inject 설정 연동).
  · 설정 끄면 hook 자동 제거(멱등).
- **dev-rebuild.ps1** — kill → build → robocopy → restart 원스텝 자동화 빌드 스크립트.

### Fixed

- **PyInstaller Tcl/Tk 번들** — `engram-overlay.spec`에 `_collect_tcl_tk()` 추가로
  `_tcl_data`/`_tk_data` 리소스 누락 경고 해소.

## [0.1.1] — 2026-07-24

### Added

- **통짜 native installer (Model B)** — `engram-overlay.exe` is now a multi-call binary
  (`--role mcp-server` / `--role kg-watcher`) so the backend runs self-referentially with
  **no conda dependency on the user machine**. Packaged as a single Inno Setup `setup.exe`
  (GUI wizard: DB path / CLI provider / Ollama model / identity / autostart) via
  `installer/build-installer.ps1`; install-time work is a lightweight `installer/configure.ps1`
  (config · MCP · shortcuts, pure PowerShell).
- **Offline embedding model bundle** — `paraphrase-multilingual-MiniLM-L12-v2` (Apache-2.0)
  is bundled into the frozen distribution, removing the HuggingFace download dependency at
  install/runtime.
- **Bubble-mode new session** — `POST /bubble/new` on the overlay STM server + an
  `engram-new-session` agent skill to explicitly reset the resident Claude session
  without restarting the overlay.

### Fixed

- **kg_sync HTTP route** returned an unawaited coroutine (`coroutine is not JSON
  serializable`), breaking kg_watcher's vault→KG auto-sync. Now awaits `kg_sync()` directly
  (thread offloading is inside the coroutine).
- **kg-watcher crash on Windows** — `_is_process_alive` used `os.kill(pid, 0)`, which on
  Windows attempts `TerminateProcess` and raises `SystemError` on dead PIDs. Replaced with
  `OpenProcess(SYNCHRONIZE)` liveness check. Backend `--role` crashes now log to stderr
  instead of a PyInstaller modal dialog.
- **Installer** — overlay is always stopped/relaunched on install (auto-update even when the
  build is skipped); Start Menu/Startup shortcuts target the `.exe` directly (enables
  "Pin to taskbar"); `__pycache__/*.pyc` no longer falsely triggers a rebuild; `Read-Host`
  prompts fall back to safe defaults under `-NonInteractive` (unattended install); installer
  scripts saved as UTF-8 BOM for Windows PowerShell 5.1 compatibility.

### Files

- engram_overlay_entry.py, overlay/main.py, overlay/stm_server.py, mcp_server.py
- scripts/kg/kg_watcher.py, core/graph/semantic/semantic_graph.py, engram-overlay.spec
- installer/build-installer.ps1, installer/configure.ps1, installer/engram-overlay.iss
- installer/common.ps1, installer/modules/{06_db,07_shims,09_overlay,10_shortcuts}.ps1
- .github/skills/engram-new-session/SKILL.md

## [2026-05-03]

### Added

- Added directive enforcement runtime settings (`directives.enforcement.mode`, `pin_top_n`, `max_items`) for configurable compliance behavior.

### Changed

- Directive selection now supports three enforcement modes: `triggered`, `hybrid`, and `always`.
- Hybrid mode now pins top-priority directives even when query triggers are absent, while capping injected directive count.
- System prompt composition now keeps directives outside ctx reference sections and marks directive blocks as highest-priority rules.
- MCP transport defaults were migrated to `streamable-http` (`/mcp`) across workspace, installer outputs, and user/global MCP config paths.
- Overlay startup now launches MCP transport from runtime config and applies listener health monitoring with bounded auto-recovery.
- MCP server `streamable-http` mode now serves a hybrid app that also exposes legacy SSE routes (`/sse`, `/messages`) during migration.

### Fixed

- Reduced multi-root transport mismatches by aligning generated client configs to HTTP endpoints, lowering chances of duplicate/stale server starts from tool UIs.
- Improved reconnect behavior after overlay/MCP restarts by adding readiness checks, recovery cooldown, and dependent-process restart orchestration.

### Files

- core/context/directives.py
- core/context/context_builder.py
- core/config/runtime_config.py
- config/config.yaml
- installer/common.ps1
- installer/modules/05_config.ps1
- mcp_server.py
- overlay/main.py

## [2026-05-01]

### Added

- Discord bot session control commands (`/session`, `/session list`, `/session use`, `/session new`, `/new`, `/newsession`, `/새세션`).
- Multi-route Discord configuration keys for guild/channel arrays, provider overrides, and scope_key override templates.
- Channel FIFO queue controls with bounded cross-channel concurrency, wait notices, and TTL-expiry notices.
- Added `claude-code-ollama` provider mode that binds Claude Code to the selected Ollama model.
- Added installer option `claude-code(ollama)` with model selection flow and dispatcher compatibility.

### Changed

- Provider selection now follows route precedence: channel override > guild override > current overlay default provider.
- Scope key resolution now follows route precedence: channel override > guild override > template > default channel scope.
- Discord runtime now keeps channel-scoped session continuity while supporting explicit new-session rollover on user command.
- Overlay provider menus now distinguish Claude direct mode and Claude-through-Ollama mode.

### Fixed

- Improved Discord runtime resilience around queued message handling and provider routing edge cases.
- Improved operational visibility with queue state and wait-position user notices.
- Hardened PyInstaller resource collection to skip Office lock/temp artifacts under `resource/character` that caused incremental build PermissionError.

### Files

- discord_bot/bot.py
- config/overlay.yaml
- overlay/config.py
- overlay/chat_window.py
- overlay/main.py
- overlay/character.py
- overlay/settings_window.py
- installer/common.ps1
- installer/modules/02_interactive.ps1
- installer/modules/07_shims.ps1
- engram-overlay.spec
- engram_overlay_entry.py
- docs/todo/todo.md

## [2026-04-30]

### Added

- Tutorial runtime warnings that explicitly require session close in step 4 phase 1 to complete continuity practice.
- Scope/session tracking fields for continuity review state (`saved_session_id`, `saved_scope_key`, `checked_session_id`).

### Changed

- Tutorial flow now enforces a strict two-phase continuity path: save-and-close first, then next-session recall verification.
- Tutorial debug bypass handling is constrained to the current step verification path only.

### Fixed

- Fixed false same-session detection in final tutorial verification by resolving active sessions with scope-aware lookup.
- Fixed STM close linkage so session close state is reflected before continuity completion checks.

### Files

- core/tutorial/progress.py
- mcp_server.py
- test/test_tutorial_runtime.py
- test/test_tutorial_session_continuity_state.py
- overlay/stm_server.py
- docs/todo/todo.md

## [2026-04-28]

### Added

- Viewport-responsive height behavior for the KG Graph dashboard panel.
- Direct iframe resizing logic for Streamlit-embedded graph content.

### Changed

- KG graph target height now follows parent viewport height (approximately 82%), clamped to a safe range.
- Graph container and tooltip pin positioning now use actual runtime container height.
- Fallback render heights were increased to provide a less cramped default layout.

### Fixed

- Resolved the issue where graph internals resized but visible area remained constrained by a fixed iframe height.

### Files

- scripts/engram_dashboard.py
- scripts/dev/engram_dashboard.py
