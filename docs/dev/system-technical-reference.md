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

- Python 런타임 환경 준비
  - Conda env(`intel_engram`) 우선
  - 불가 시 프로젝트 `.venv`
- MCP 설정 생성
  - `~/.copilot/mcp-config.json` (Copilot CLI)
  - `~/.claude.json` (Claude Code)
  - `~/.engram/claude-mcp.json` (Claude Code shim용)
  - `%APPDATA%/Code/User/mcp.json` (VSCode 전역)
  - `.vscode/mcp.json` (VSCode 워크스페이스)
  - `.mcp.json` (프로젝트 로컬)
- 사용자 설정/템플릿
  - `~/.engram/user.config.yaml`
  - `~/.engram/overlay.user.yaml`
- DB 초기화
  - `<db.root_dir>/engram.db`
- Wiki vault 초기화
  - `<db.root_dir>/docs/` 하위 디렉토리 + HOME + templates + guide
- CLI shim 생성 (`~/.engram/*.cmd`)
  - engram-copilot, engram-gemini, engram-claude, engram-goose, engram-overlay
- Copilot skill 배포
  - `.github/skills/engram/SKILL.md` → `~/.copilot/skills/engram/SKILL.md`
- 오버레이 빌드 시도
  - `engram-overlay.spec` 기준 PyInstaller 빌드
  - 빌드 실패 시 로그 저장 후 나머지 설치는 계속 진행

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

### 9.4 Layer 3 런타임 지시문

`core/context_builder.py`의 `build_system_prompt()` 가 모든 클라이언트에 공통으로 주입한다:

- DB `directives` 테이블에서 `active=true` 항목 로드
- 정체성, 페르소나, 기억 요약, 궁금증 항목과 함께 조립
- overlay 경유 클라이언트(Copilot CLI, Gemini)는 이 레이어만 적용됨

### 9.5 `config/clients/` 파일 수정 시

수정 후 `installer/install.ps1` 재실행으로 배포 대상에 반영해야 한다.
`~/.engram/copilot-instructions.md`는 설치 시 자동으로 덮어쓰여진다 — 직접 편집하지 말고 `config/clients/copilot.md`를 수정할 것.
