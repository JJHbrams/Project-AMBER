# Engram System Technical Reference

이 문서는 구현 기준의 기술 레퍼런스다.
사용자 설치/사용 안내는 루트 README를 기준으로 본다.

## 1. Runtime Entry Points

### Active

- `~/.engram/engram-copilot.cmd`
  - Copilot CLI 진입점
  - `--overlay`, `--overlay-stop` 파싱
- `~/.engram/engram-gemini.cmd`
  - Gemini CLI 진입점
- `~/.engram/engram-codex.cmd`
  - Codex CLI 진입점
- `~/.engram/engram-claude.cmd`
  - Claude Code CLI 진입점
- `~/.engram/engram-overlay.cmd`
  - `dist/engram-overlay.exe` 실행 런처 (exe 빌드 성공 시 실사용)

설치 스크립트 근거: `installer/install.ps1`의 shim 생성 섹션

### Deprecated / Legacy

- `engram.py`
  - 레거시 독립 REPL 진입점
  - 현재는 안내 메시지 출력 후 `sys.exit(1)`
- `core/cli_bridge.py`
  - 레거시 claude subprocess 경로
- `scripts/run.bat`
  - 레거시 REPL 실행 경로 (현행 진입점 아님)

핵심 원칙: 현재 표준 진입점은 `~/.engram/*.cmd` shim이다.

## 2. Install Side Effects (installer/install.ps1)

설치 시 다음이 자동으로 구성된다.

### 2.1 Frozen Overlay Build and Release Validation

- 개발 재빌드, source installer의 overlay 단계, release `build-installer.ps1`는
  `installer/build-overlay.ps1`을 공유한다.
- `auto`, `rebuild`, `clean`, `skip` 모드를 지원한다. 빌드는 매번 고유한 임시
  dist에서 수행하고, 성공적인 manifest 생성과 `embedding-check` 및
  overlay/dashboard smoke 이후에만 `dist` 또는 별도 deploy 대상에 교체한다.
- `auto`/`rebuild`의 PyInstaller 증분 빌드 실패는 clean 빌드로 한 번 재시도한다.
  결정적인 manifest/smoke 실패에는 긴 clean 빌드를 반복하지 않는다. 실패하면
  기존 working artifact를 유지하며, `-NoStart`가 없을 때만 성공 결과를 재시작한다.
- 번들의 `build-manifest.json`은 Python·PyInstaller·MCP·
  sentence-transformers 버전, source/config/resource 해시, 임베딩 모델 manifest를
  기록한다. `build-installer.ps1 -SkipBuild`는 이 검증이 실패하면 패키징하지 않는다.
- `engram-overlay.exe --role smoke-check`는 listener를 열지 않고 MCP의 실제
  `mcp.server.fastmcp.FastMCP` import와 mcp-server, kg-watcher, overlay import 및
  임베딩 로드를 확인한다.
- `engram-dashboard.exe --smoke-check`는 Streamlit AppTest로 `Overview` 렌더를
  확인한다. 이어 임시 포트에서 실제 sidecar를 시작해 `/_stcore/health`가 `ok`인지
  확인하고 정확한 PID만 종료한다. release packaging은 ISCC 전에 렌더 검사를 재실행한다.
- `resource/embedding-model/manifest.json`은 모델 ID, offline snapshot revision,
  sentence-transformers 버전, exported file SHA-256을 기록한다. manifest가 유효한
  cached/exported 모델은 네트워크 다운로드나 재export 없이 사용하고, frozen runtime도
  파일 hash를 검증한 뒤 로드한다.

- Python 런타임 환경 준비
  - Conda env(`intel_engram`) 우선
  - 불가 시 프로젝트 `.venv`
- MCP 설정 생성
  - `~/.copilot/mcp-config.json` (Copilot CLI)
  - `~/.codex/config.toml` (`codex mcp add`, 기존 `engram` 항목 보존)
  - `~/.claude.json` (Claude Code)
  - `~/.engram/claude-mcp.json` (Claude Code shim용)
  - `%APPDATA%/Code/User/mcp.json` (VSCode 전역)
  - `.vscode/mcp.json` (VSCode 워크스페이스)
  - `.mcp.json` (프로젝트 로컬)
- 사용자 설정/템플릿
  - `~/.engram/user.config.yaml`
  - `~/.engram/overlay.user.yaml`
- Claude Code managed hooks
  - `~/.claude/settings.json`의 Engram 관리 `SessionStart` / `PreToolUse` 항목만 멱등 동기화
  - `~/.engram/engram-sessionstart-hook.ps1`
  - `~/.engram/engram-claude-pretool-hook.ps1`
  - `SessionStart`는 `session.auto_inject=true`일 때만 설치
  - `PreToolUse`는 `directives.policy.guidance_level != off`일 때 설치된다.
  - `warn`은 repo-write 위험을 경고·추천·audit만 하고, `enforce_agents`는 유효한
    정책 위반 tool call을 `permissionDecision: deny`로 차단한다.
- Codex managed hooks
  - `~/.codex/hooks.json`의 Engram 관리 `PreToolUse` handler만 멱등 동기화
  - `Bash`와 `apply_patch` repo-write를 공통 policy preflight로 평가
  - `warn` 위험은 `hookSpecificOutput.additionalContext`, `enforce_agents`의 유효한
    정책 위반은 `permissionDecision: deny`로 전달
  - user hook은 Codex `/hooks`에서 최초 및 내용 변경 시 신뢰 승인이 필요
- Repository managed Git advisor
  - `engram_get_context_once` 세션 bootstrap은 cwd가 Git 저장소이고 정책 가이드가 켜져
    있으면 공용 Git 디렉터리의 managed advisor를 멱등 설치한다.
  - `engram-overlay.exe --role git-hook install|status|uninstall --repo <path>`로 명시 관리도 가능하다.
  - uninstall은 공용 Git 디렉터리에 opt-out marker를 남기며, 명시적 install만 이를 제거한다.
  - advisor 활성화 중에는 repo-local `merge.ff=false`를 적용하고, 정책 가이드 OFF 또는
    uninstall 시 관리 전 값을 복원한다.
  - Claude·Codex agent hook은 `git merge`에 `--no-ff`를 요구하고 `--ff-only`를 거부한다.
  - 기존 non-Engram `pre-commit` 또는 custom `core.hooksPath`가 있으면 설치를 거부한다.
  - 관리 hook은 POSIX `sh` wrapper에서 provider-neutral backend role을 직접 호출한다.
    PowerShell에 의존하지 않으며 Git Bash·WSL·native Linux를 지원한다.
  - 정상 설치된 managed wrapper의 repo 경로/backend/권한/실행 오류는 모두 경고 후
    `exit 0`으로 처리한다. 사용자가 hook 파일 자체를 교체·손상한 경우는 보장 범위 밖이다.
  - 보호 브랜치 위험은 콘솔에 안내하고 audit에 기록한다. 명시적 maintenance 맥락은
    선택적으로 `ENGRAM_CHORE_INTENT=1`과 `ENGRAM_CHORE_REASON`으로 남길 수 있다.
  - 설정 GUI에서 정책 가이드를 끄면 `~/.engram/policy-guidance.disabled` marker를 먼저
    생성해 설치된 repo advisor가 backend를 시작하지 않고 검사·audit도 건너뛴다.
- DB 초기화
  - `<db.root_dir>/engram.db`
- Wiki vault 초기화
  - `<db.root_dir>/docs/` 하위 디렉토리 + HOME + templates + guide
- CLI shim 생성 (`~/.engram/*.cmd`)
  - engram-copilot, engram-gemini, engram-claude, engram-goose, engram-overlay
- Copilot skill 배포
  - `.github/skills/engram/SKILL.md` → `~/.copilot/skills/engram/SKILL.md`
- 오버레이 빌드 시도
  - `installer/build-overlay.ps1` 기준 manifest 검증·PyInstaller·역할 smoke 테스트
  - 빌드 실패 시 기존 artifact를 보존하고 나머지 설치는 계속 진행

**설치 시 건드리지 않는 것:**

- `~/.claude/CLAUDE.md` — 사용자 전역 Claude 지침. shim이 `--append-system-prompt`로 대체
- Goose `config.yaml instructions` 필드 — shim이 `GOOSE_MOIM_MESSAGE_TEXT`로 대체

## 3. Storage Architecture

### 3.1 SQLite

메인 상태 저장소:

- identity / persona
- sessions / messages / memories
- working_memory
- curiosities / directives
- activity_log
- discord_queue
- kg_nodes / kg_edges

초기화: `core/db.py` (`initialize_db()`)

### 3.2 KuzuDB Semantic Layer

경로: `<db.root_dir>/semantic_graph/`

역할:

- KG 노드 임베딩 저장
- 시맨틱 유사도 검색
- Cypher 질의

핵심 모듈: `core/semantic_graph.py`

### 3.3 Wiki Vault

경로: `<db.root_dir>/docs/`

역할:

- 마크다운 원문 지식 저장
- `kg_sync` / watcher를 통해 SQLite + Kuzu 레이어와 동기화

관련 스크립트:

- `scripts/kg_sync.py`
- `scripts/kg_watcher.py`

## 4. Config Layering

### 4.1 Runtime Config (`core/runtime_config.py`)

로드 순서:

1. 내장 기본값
2. `config/config.yaml`
3. `~/.engram/runtime.user.yaml` (legacy)
4. `~/.engram/user.config.yaml` (권장)

주요 키:

- `db.root_dir`
- `memory.scope.*`
- `memory.short_term.*`
- `memory.working.*`
- `directives.policy.guidance_level` (`off` | `warn` | `enforce_agents`)
  - `enforce_agents`도 사람의 repo-local Git advisor는 차단하지 않는다.
  - legacy `guidance_enabled`와 `claude_pretool_enforcement`는 layer별 merge 전에
    canonical level로 읽기 호환
- `copilot.model`
- `copilot.allow_all_tools`

### 4.2 Persona Merge (`core/identity.py`)

필드 우선순위:

1. `~/.engram/persona.user.yaml` (값 있는 필드)
2. DB persona 진화값
3. `config/persona.yaml` (값 있는 필드)
4. 기본값

수치 필드(`warmth`, `formality`, `humor`, `directness`)는 EMA 블렌딩(`alpha=0.3`)으로 업데이트된다.

## 5. STM Broker and Overlay

### 5.1 Overlay Runtime

`overlay/main.py`에서 수행:

- 트레이 아이콘 + 핫키
- 캐릭터 오버레이
- 터미널 챗 창
- STM HTTP 서버 시작
- Discord 봇 조건부 시작

### 5.2 STM HTTP Broker

`overlay/stm_server.py`:

- 기본 포트: `17384`
- 주요 엔드포인트:
  - `POST /stm/session/start`
  - `POST /stm/message`
  - `GET /stm/messages`
  - `POST /stm/session/close`
  - `GET /health`

`mcp_server.py`는 시작 시 broker 연결을 시도하고,
연결 가능하면 broker 모드, 아니면 direct SQLite 모드로 동작한다.

## 6. Discord Integration (Actual Behavior)

현재 구현(`discord_bot/bot.py`)은 다음과 같다.

- 오버레이 실행 중 봇 lifecycle 유지
- 멘션 수신 시 즉시 `🕐` 리액션
- 별도 스레드에서 Copilot CLI 호출로 응답 생성
- 채널에 응답 전송 후 `✅` 리액션 교체
- `discord_queue`에는 기록 목적 데이터 저장

주의:

- README에서 과거 큐-폴링 중심 설명만 단독으로 기술하면 실제 동작과 불일치가 생긴다.
- MCP 도구(`engram_discord_read_queue`, `engram_discord_mark_processed`, `engram_discord_send`)는 수동/통합 시나리오에서 계속 사용 가능하다.

## 7. MCP Tools

`mcp_server.py` 기준 `@engramMCP.tool()` 선언 수는 46개다.

대표 카테고리:

- 상태/컨텍스트
- 정체성/페르소나/테마
- 기억/세션/반성
- 호기심/지침/활동
- Discord
- KG(구조 + 시맨틱)

정확한 목록은 `mcp_server.py`를 source-of-truth로 본다.

## 8. Documentation Policy

- 루트 README: 설치/실행/온보딩 중심
- `docs/dev/*`: 아키텍처, 내부 동작, 운영 디테일, deprecated 노트
- 기능 변경 시 README와 기술문서를 함께 갱신한다.

## 9. AI Instruction Layer

LLM 클라이언트별 지침 구조는 3계층으로 나뉜다.

### 9.1 계층 구조

| 계층                            | 역할                                               | 파일                                                 |
| ------------------------------- | -------------------------------------------------- | ---------------------------------------------------- |
| **Layer 1 — 세션 프로토콜**     | 클라이언트 부트스트랩: engram 로드, 세션 종료 처리 | `config/clients/*.md` → 설치 시 배포                 |
| **Layer 2 — 프로젝트 컨텍스트** | 이 레포지토리의 구조·기술 스택·개발 규칙           | `CLAUDE.md`                                          |
| **Layer 3 — 런타임 지시문**     | 운영 규칙 (DB에 저장, 모든 클라이언트에 주입)      | engram DB `directives` 테이블 → `context_builder.py` |

### 9.2 클라이언트별 지침 소스

| 클라이언트              | 세션 프로토콜 소스              | 배포 대상                                         | 프로젝트 컨텍스트           |
| ----------------------- | ------------------------------- | ------------------------------------------------- | --------------------------- |
| Claude Code             | `config/clients/claude-code.md` | `~/.claude/CLAUDE.md` (전역)                      | `CLAUDE.md` (프로젝트 로컬) |
| Goose                   | `config/clients/goose.md`       | `~/.config/goose/config.yaml` `instructions` 필드 | 별도 없음                   |
| Copilot CLI             | `config/clients/copilot.md`     | `~/.engram/copilot-instructions.md`               | 없음 (Layer 3 지시문만)     |
| Overlay 경유 클라이언트 | —                               | —                                                 | Layer 3 지시문만 적용       |

### 9.3 배포 메커니즘

`installer/install.ps1` 실행 시:

- **Claude Code**: `config/clients/claude-code.md` → `~/.claude/CLAUDE.md` 로 복사 (cwd 치환 포함)
- **Goose**: `config/clients/goose.md` → `~/.config/goose/config.yaml`의 `instructions` 필드에 주입
- **Copilot**: `config/clients/copilot.md` → `~/.engram/copilot-instructions.md` 로 복사
  - `COPILOT_CUSTOM_INSTRUCTIONS_DIRS=%USERPROFILE%\.engram` 으로 로드됨
  - Copilot CLI에 `--append-system-prompt` 플래그가 없어 파일 경유 방식 사용
- **Copilot Skill**: `.github/skills/engram/SKILL.md` → `~/.copilot/skills/engram/SKILL.md` 로 복사 (`/engram` 슬래시 커맨드 활성화)

## 10. Rebuild and Deploy — 이미 설치된 환경에 개발 변경 적용

이미 설치가 끝난 상태에서 코드만 바뀌었을 때 무엇을 돌려야 하는가.
운영 시점 판단 규칙은 `.claude/skills/engram-rebuild/SKILL.md` 에도 같은 내용이 있다.

### 10.1 왜 exe 하나만 갈아끼우면 되는가 — 멀티콜 바이너리

frozen 빌드에서는 **같은 exe 가 `--role` 인자에 따라 백엔드까지 겸한다**
(`engram_overlay_entry.py::_dispatch_backend_role`). conda python 없이 백엔드가 자립하는 구조.

| 역할      | frozen 실행                                 | 소스(dev) 실행                    |
| --------- | ------------------------------------------- | --------------------------------- |
| overlay   | `engram-overlay.exe`                        | `python engram_overlay_entry.py`  |
| MCP 서버  | `sys.executable --role mcp-server`          | `<conda python> mcp_server.py`    |
| kg-watcher| `sys.executable --role kg-watcher`          | `<conda python>` 스크립트 실행    |
| policy preflight | `sys.executable --role policy-preflight` | `python engram_overlay_entry.py --role policy-preflight` |
| Git hook manager | `sys.executable --role git-hook <op> --repo <path>` | `python engram_overlay_entry.py --role git-hook <op> --repo <path>` |

따라서 `overlay/` 든 `core/` 든 **모든 Python 소스가 이 exe 하나에 번들된다.**
exe 를 교체하면 overlay·MCP·kg-watcher 가 한 번에 갱신된다.

> `overlay.user.yaml` 의 `mcp.python_exe` 는 **소스 모드에서만** 쓰인다.
> frozen 설치본에서 이 값을 바꿔도 MCP 동작에 영향이 없다.

### 10.2 판단 — dev-rebuild vs 전체 install

**Python 소스만 바뀌었으면 `dev-rebuild.ps1` 로 충분하다.**

전체 `INSTALL.ps1` 이 필요한 경우:

| 변경 대상                            | 이유                                                                          |
| ------------------------------------ | ----------------------------------------------------------------------------- |
| `requirements.txt` / `environment.yml` | PyInstaller 는 conda env 에서 패키지를 수집한다. 새 의존성이 env 에 없으면 번들에도 안 들어감 (모듈 03/04) |
| shim(`~/.engram/*.cmd`) · PATH · 바로가기 | 모듈 07/08/10                                                                 |
| MCP 클라이언트 등록 정보 · 클라이언트 지침(`config/clients/*.md`) | 모듈 05, §9.3                                             |

DB 스키마는 대개 예외다 — `core/storage/db.py` 가 연결 시 `CREATE TABLE IF NOT EXISTS`
마이그레이션을 수행한다(`activity_log` 도 이 경로로 생성됨). 별도 마이그레이션 스크립트가
필요한 변경일 때만 모듈 06 이 필요하다.

### 10.3 두 경로의 빌드 안전성 차이

| | `dev-rebuild.ps1` | `INSTALL.ps1` (모듈 09) |
| --- | --- | --- |
| 엔진 | `installer/build-overlay.ps1` | `installer/build-overlay.ps1` |
| 빌드 대상 | 고유 임시 distpath → 성공 시 교체 | 고유 임시 distpath → 성공 시 교체 |
| 실패 시 | 기존 dist 보존, PyInstaller 증분 실패만 clean 으로 1회 자동 재시도 | 기존 dist 보존, PyInstaller 증분 실패만 clean 으로 1회 자동 재시도 |
| 범위 | 빌드 + 재기동 | 10개 모듈 전체 |

평상시엔 `dev-rebuild.ps1`, 빌드가 꼬이면 `.\INSTALL.ps1 -OverlayBuildMode clean` 으로 정리한다.

### 10.4 `-OverlayBuildMode` 의미

`INSTALL.ps1` 기본값은 `auto` 이며, shared engine의 `build-manifest.json`이
Python 및 package 버전과 source/config/resource SHA-256을 검증해 빌드 여부를 정한다.
`build-installer.ps1 -SkipBuild`도 같은 검증을 통과해야 한다.

| 값        | 동작                                        |
| --------- | ------------------------------------------- |
| `auto`    | build manifest 검증 후 필요할 때만 빌드 (기본값) |
| `rebuild` | 항상 증분 빌드                              |
| `clean`   | 항상 clean 빌드                             |
| `skip`    | 빌드 생략 (설치의 나머지 단계만)            |

### 10.5 절차상 주의

- shared engine은 **실행 중인 overlay 를 전부 종료**한 뒤 첫 인스턴스의 경로를
  deploy 대상으로 기억한다. 새 번들은 고유 임시 디렉터리에서 완성·검증한 뒤
  deploy 디렉터리와 원자적으로 교체하며 실패 시 기존 번들을 복원한다.
- overlay 종료는 STM 브로커·MCP·kg-watcher 동반 종료를 의미한다. 다른 CLI 세션이
  engram MCP 를 쓰는 중이면 그 세션의 MCP 호출이 실패한다.
- 사용자 설정은 보존된다. 모듈 09 는 `overlay.user.yaml` 이 **없을 때만** 템플릿을 만든다.

### 9.4 Layer 3 런타임 지시문

`core/context_builder.py`의 `build_system_prompt()` 가 모든 클라이언트에 공통으로 주입한다:

- DB `directives` 테이블에서 `active=true` 항목 로드
- 정체성, 페르소나, 기억 요약, 궁금증 항목과 함께 조립
- overlay 경유 클라이언트(Copilot CLI, Gemini)는 이 레이어만 적용됨

### 9.5 `config/clients/` 파일 수정 시

수정 후 `installer/install.ps1` 재실행으로 배포 대상에 반영해야 한다.
`~/.engram/copilot-instructions.md`는 설치 시 자동으로 덮어쓰여진다 — 직접 편집하지 말고 `config/clients/copilot.md`를 수정할 것.
