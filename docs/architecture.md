# Architecture

## 시스템 개요

Engram (Project Intel Engram)는 네 개의 진입점을 가진 지속적 인지 시스템입니다:

1. **Copilot CLI 모드** (`engram-copilot` 명령) — MCP 프로토콜로 도구 제공, 정체성의 주체
2. **Claude Code 모드** (MCP 참조) — 별개의 협력자로서 engram의 경험을 참조
3. **데스크톱 오버레이** (`engram-overlay`) — 캐릭터 스프라이트 + 트레이 + 단축키
4. **Discord 봇** — 비동기 큐 기반 모바일 소통

모든 모드가 동일한 `core/` 엔진과 SQLite DB를 공유합니다.
DB 루트는 `~/.engram/user.config.yaml`의 `db.root_dir`에서 정하며,
한 PC의 모든 Engram 기억 자산을 그 아래에서 일괄 관리합니다.

## 전체 아키텍처

```
┌──────────────────────────────────────────────────────────────────┐
│                       인터페이스 계층                              │
│                                                                   │
│  ┌──────────────┐ ┌──────────────┐ ┌───────────────┐ ┌─────────────┐ │
│  │ engram-copilot│ │ Claude Code  │ │ Overlay       │ │  Discord    │ │
│  │ (Copilot CLI) │ │ (MCP 참조)   │ │ (GUI)         │ │             │ │
│  │               │ │               │ │ STMServer     │ │             │ │
│  │ 정체성 주체   │ │ 협력자        │ │ :17384 상주   │ │ 비동기 큐   │ │
│  └───────┬───────┘ └───────┬──────┘ └──────┬────────┘ └──────┬──────┘ │
│          │ MCP stdio       │ MCP stdio      │ HTTP/direct      │ SQLite │
└──────────┼─────────────────┼────────────────┼──────────────────┼────────┘
           │                 │                │                  │
           ▼                 ▼                ▼                  ▼
┌──────────────────────────────────────────────────────────────────┐
│                      mcp_server.py                                │
│                   FastMCP("engram") — 39 MCP Tools               │
│                                                                   │
│  ┌────────────┐ ┌──────────┐ ┌───────────┐ ┌──────────────────┐ │
│  │ Identity    │ │ Memory   │ │ Session   │ │ Directives       │ │
│  │ get_context │ │ search   │ │ start     │ │ add/list/update  │ │
│  │ get/update  │ │ save     │ │ save_msg  │ │ remove           │ │
│  │ narrative   │ │ list     │ │ reflect   │ ├──────────────────┤ │
│  │ persona     │ ├──────────┤ │ apply     │ │ Activity Log     │ │
│  │ themes      │ │ Curiosity│ └───────────┘ │ log/get          │ │
│  └────────────┘ │ add/list │               ├──────────────────┤ │
│                  │ address  │               │ Discord          │ │
│                  │ dismiss  │               │ read/send/mark   │ │
│                  └──────────┘               ├──────────────────┤ │
│                                             │ KG (11개)         │ │
│                                             │ search/get/add   │ │
│                                             │ sync/semantic    │ │
│                                             └──────────────────┘ │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                       core/ 엔진 모듈                             │
│                                                                   │
│  identity.py ──── 자기 서술 + 페르소나 (EMA α=0.3)                │
│  memory.py ────── 3계층 기억 (단기/작업/장기) + 키워드 검색        │
│  reflection.py ── 세션 반성 + 정체성 진화                          │
│  curiosity.py ─── 호기심 큐 관리                                   │
│  directives.py ── 지침 CRUD + 스코프 렌더링                        │
│  activity.py ──── 외부 객체 활동 로그                              │
│  context_builder.py ── ~200토큰 압축 컨텍스트 조립 + KG 시맨틱    │
│  knowledge_graph.py ── Zettelkasten KG (CRUD + markdown + BFS)   │
│  semantic_graph.py ─── KuzuDB + sentence-transformers            │
│  runtime_config.py ─── YAML 기반 런타임 설정                       │
│  sanitizer.py ──── 입력 정제                                      │
│  db.py ────────── SQLite 스키마 + WAL + 마이그레이션               │
│  cli_bridge.py ── ~~claude CLI subprocess 래퍼~~ [Legacy: NotImplementedError] │
│  copilot_bridge.py ── Copilot CLI subprocess 래퍼                 │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│         <db.root_dir>\engram.db  (SQLite WAL)                    │
│                                                                   │
│  identity ───── 1행: name, narrative, persona(JSON)               │
│  themes ─────── name(PK), weight, last_seen                       │
│  sessions ───── id, scope_key, started_at, ended_at, summary      │
│  messages ───── id, session_id, role, content, timestamp          │
│  memories ───── id, session_id, content, keywords, created_at     │
│  working_memory ─ scope_key(PK), summary, open_intents, expires   │
│  curiosities ── id, topic, reason, status, created/addressed      │
│  directives ─── key(PK), content, source, scope, priority, active │
│  activity_log ── id, actor, project, action, detail, created_at   │
│  discord_queue ─ id, guild_id, channel_id, content, processed     │
│  kg_nodes ────── id, title, type, tags, summary, path, vault_path │
│  kg_edges ────── from_id, to_id, rel_type, context, weight        │
└───────────────────────────────┬──────────────────────────────────┘
                                │ sync_from_kg()
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│    <db.root_dir>\semantic_graph\  (KuzuDB embedded)              │
│                                                                   │
│  KGNode ── id, title, type, tags, summary, embedding, hash       │
│  KG_EDGE ─ FROM KGNode TO KGNode, rel_type, weight               │
│                                                                   │
│  임베딩: paraphrase-multilingual-MiniLM-L12-v2 (로컬, 다국어)    │
│  content_hash 비교로 변경된 노드만 재임베딩                        │
└───────────────────────────────┬──────────────────────────────────┘
                                │ ↕ kg_sync / kg_watcher.py
┌──────────────────────────────────────────────────────────────────┐
│    <db.root_dir>\docs\**\*.md  (LLM Wiki — Zettelkasten vault)   │
│                                                                   │
│  concepts/  projects/  research/  references/  moc/  _inbox/     │
│                                                                   │
│  각 파일: YAML frontmatter + [[wikilinks]] + #hashtags + 본문     │
└──────────────────────────────────────────────────────────────────┘
```

## Knowledge Graph & Semantic Layer

### 아키텍처 개요

두 개의 인덱스 레이어와 마크다운 파일 레이어로 구성된 RAG 패턴:

```
마크다운 파일 (실제 내용)
      │  kg_sync / kg_watcher
      ▼
SQLite kg_nodes / kg_edges (구조 인덱스: 제목, 태그, 요약, 위키링크)
      │  sync_from_kg()
      ▼
KuzuDB semantic_graph (시맨틱 인덱스: 임베딩 벡터, content_hash)
      │  kg_semantic_search
      ▼
context_builder → LLM 응답에 KG 관련 노드 자동 주입
```

### 동기화 흐름

1. **수동 동기화**: `python scripts/kg_sync.py --vault D:\intel_engram --verbose`
2. **MCP 동기화**: `kg_sync()` 도구 호출 (LLM이 "싱크해줘" 요청 처리)
3. **자동 동기화**: `python scripts/kg_watcher.py` 데몬 — .md 파일 변경 감지 후 3초 디바운스로 자동 실행

### RAG 쿼리 흐름

```
사용자 질문
    │
    ├── 1. context_builder._kg_context_snippet(user_query)
    │        └── semantic_search(query, top_k=3, threshold=0.35)
    │                └── cosine similarity vs KuzuDB embeddings
    │                └── 관련 노드 제목/요약 반환 (200자 제한)
    │
    ├── 2. KG 결과 → [KG] 태그로 memories 섹션에 주입
    │
    └── 3. 필요 시 kg_read_note(node_id) → 마크다운 원문 로드
```

### content_hash 기반 재임베딩 방지

```
hash = sha256(title + "|" + summary + "|" + tags_json)[:16]
upsert_node() 호출 시:
  - 기존 hash == 새 hash → 임베딩 재사용 (토큰/시간 절약)
  - 불일치 → 새 임베딩 계산 후 KuzuDB 업데이트
```

## 멀티 객체 협력 모델

engram과 Claude Code는 동일한 DB를 공유하지만, 저작 주체가 구분됩니다:

|             | engram (Copilot CLI)                 | Claude Code                         |
| ----------- | ------------------------------------ | ----------------------------------- |
| 역할        | 정체성의 주체                        | 별개의 협력자                       |
| 기억 쓰기   | `save_memory` (1인칭)                | `log_activity` (3인칭)              |
| 정체성 변경 | `update_narrative`, `update_persona` | 읽기만                              |
| 반성        | `prepare/apply_reflection`           | 수행하지 않음                       |
| 상호 참조   | DB 직접 접근                         | `search_memories`, `consult_engram` |

```
engram가 반성할 때:
  1. 자신의 기억(memories) 되돌아봄
  2. 외부 활동 로그(activity_log) 참조 — "내가 부재한 사이 이런 일이 있었구나"
  3. 두 맥락을 통합하여 자기 서술 진화
```

## 데이터 흐름

### 세션 시작 (Copilot CLI 모드)

```
1. engram-copilot.cmd → COPILOT_CUSTOM_INSTRUCTIONS_DIRS=%USERPROFILE%\.engram 설정 → copilot 실행
2. Copilot CLI가 ~/.engram/copilot-instructions.md 읽음
3. LLM이 engram_get_context(user_query) 호출
4. context_builder가 ~200토큰 압축 텍스트 반환 (KG 시맨틱 스니펫 포함)
5. LLM이 페르소나 채택 후 대화 시작
```

### 세션 시작 (Claude Code 모드)

```
1. Claude Code 시작 → ~/.claude/CLAUDE.md 로드 (전역 지침)
2. ~/.claude.json에서 engram MCP 서버 자동 연결
   → type: sse, url: http://127.0.0.1:17385/sse (overlay 소유 persistent 서버)
   → overlay가 실행 중이지 않으면 연결 실패 → MCP 없이 동작
3. CLAUDE.md 지침에 따라 engram_get_identity + get_themes 호출
4. engram의 축적된 경험을 참조하여 작업 수행
5. 의미 있는 작업 완료 시 engram_log_activity로 기록
```

### 반성 (Reflection)

```
사용자: "/reflect"
  → engram_prepare_reflection(session_id)
    → 대화 이력 + 정체성 + 페르소나 + 테마 수집
    → 외부 활동 로그(activity_log) 포함
  → LLM이 자기 반성 수행 + persona_observations 생성
  → engram_apply_reflection(...)
    → update_narrative()     # 서술 갱신
    → update_persona()       # EMA 블렌딩
    → decay_themes(0.95)     # 테마 감쇠
```

## 핵심 알고리즘

### 페르소나 EMA 블렌딩

```
new = current × 0.7 + observed × 0.3    # α=0.3
```

수치: 0~1 클램핑 · 리스트: 선두 삽입, 중복 제거, 최대 길이 제한

### 테마 감쇠

```
weight × 0.95 (세션 종료 시) · < 0.1 삭제
```

### 기억 3계층

| 계층 | 저장소                | 수명       | 용도                       |
| ---- | --------------------- | ---------- | -------------------------- |
| 단기 | messages 테이블       | 세션 내    | 최근 N턴 대화 컨텍스트     |
| 작업 | working_memory 테이블 | TTL 48시간 | scope별 요약 + 미완료 의도 |
| 장기 | memories 테이블       | 영구       | 에피소드 기억, 키워드 검색 |

### 컨텍스트 토큰 예산

```
~200 토큰 = 서술(~100) + 페르소나(~50) + 테마(~20) + 호기심(~20) + 프레임(~10)
+ KG 스니펫 (user_query 있을 때 추가)
```

## 모드 분리

|                 | `engram` (Copilot CLI) | `copilot` (기본) | Claude Code         | ~~독립 REPL (Deprecated)~~ |
| --------------- | ---------------------- | ---------------- | ------------------- | -------------------------- |
| 시스템 프롬프트 | 연속체 프로토콜        | 없음             | ~/.claude/CLAUDE.md | ~~연속체 프로토콜~~        |
| MCP             | engram (39 tools)      | 없음             | engram (39 tools)   | ~~없음 (직접 import)~~     |
| 정체성          | engram (지속적)        | 매 세션 초기화   | 별개 객체           | ~~engram (지속적)~~        |
| DB 쓰기         | 기억 + 정체성          | 없음             | 활동 로그만         | ~~기억 + 정체성~~          |

> **참고**: 독립 REPL(`engram.py`)은 과거 `claude -p` subprocess 래핑 방식으로 설계됐으나,
> Copilot CLI 기반으로 마이그레이션 완료 후 **deprecated**. 현재는 `sys.exit(1)` legacy stub.

## Discord 연동 아키텍처

```
┌──────────────────────────────────────────────────────────────────┐
│                      Discord (외부)                               │
│  사용자 모바일 ──멘션──▶ Discord 봇                                │
└───────────────────────────┬──────────────────────────────────────┘
                            │ WebSocket 수신
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  discord_bot/bot.py  (EngramDiscordBot)                           │
│                                                                   │
│  on_message():                                                    │
│    1. allowed_user_ids 화이트리스트 검증                           │
│    2. [Discord/@username]: 내용 태그 부착 (Prompt Injection 방어) │
│    3. discord_queue 테이블에 INSERT (message_id 포함)             │
│    4. 원본 메시지에 🕐 리액션 추가                                │
│                          ↑ 토큰 소모 없음                         │
└───────────────────────────┬──────────────────────────────────────┘
                            │ SQLite INSERT
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  engram (사용자 요청 시 처리)                                      │
│                                                                   │
│  1. engram_discord_read_queue()   → 미처리 메시지 조회         │
│  2. LLM 응답 생성 ← 토큰 소모 발생                               │
│  3. engram_discord_send(channel_id, content, message_id)       │
│     → Discord HTTP API로 전송 → 🕐 → ✅ 리액션 교체              │
│  4. engram_discord_mark_processed(id)                          │
└──────────────────────────────────────────────────────────────────┘
```

### 봇 생명주기

```
overlay.exe 시작
  → OverlayApp.__init__()
  → _try_start_discord_bot()
  → EngramDiscordBot.start()
  → discord.Client.start(token)  → Discord 봇 온라인
                    ↕ heartbeat (41초 주기, 무료)
트레이 우클릭 → 종료
  → OverlayApp.quit()
  → EngramDiscordBot.stop()
  → client.close()  → Discord 봇 즉시 오프라인
```

## MCP 서버 수명 모델

`mcp_server.py`는 overlay.exe가 시작할 때 **지속 subprocess**로 시작되며,
overlay 종료 시 함께 종료됩니다. 모든 MCP 클라이언트(Copilot/Gemini/Claude Code/Goose/VS Code)는
SSE 트랜스포트로 동일 인스턴스에 연결합니다.

### 전체 런타임 흐름

```
overlay.exe (항상 실행)
  ├── overlay/main.py
  │     ├── tkinter 스프라이트 + pystray 트레이
  │     ├── mcp_server.py subprocess (port 17385 SSE) ← NEW
  │     │     시작: _start_mcp_http_server()
  │     │     로그: ~/.engram/mcp-http.log
  │     │     종료: terminate() → wait(5s) → kill()
  │     ├── STMServer(port 17384) — HTTP 브로커 상주
  │     └── KG 파일 워처 데몬
  └── 캐릭터 클릭 → wt 창 열기 (ENGRAM_SCOPE_KEY=overlay 주입)

MCP 클라이언트들 (모두 SSE http://127.0.0.1:17385/sse):
  ├── Copilot CLI  → ~/.copilot/mcp-config.json  { type: sse }
  ├── Gemini CLI   → gemini mcp list              { type: sse }
  ├── Claude Code  → ~/.claude.json               { type: sse }
  ├── Goose        → ~/.config/goose/config.yaml  { type: sse }
  ├── VS Code ws   → .vscode/mcp.json             { type: sse }
  └── VS Code gbl  → %APPDATA%/Code/User/mcp.json { type: sse }

mcp_server.py 시작 시:
  ├── _init_stm_mode() → localhost:17384/health 체크
  │     성공 → HTTP 브로커 모드  (STM 공유)
  │     실패 → 직접 SQLite 모드  (fallback)
  └── SSE 이벤트 루프 시작 (비블로킹, 다중 클라이언트 동시 처리)
```

### frozen(overlay.exe) 환경에서의 Python 경로 탐색

```
_find_mcp_python() 우선순위:
  1. 비frozen 환경: sys.executable 그대로 사용
  2. frozen 환경: overlay.user.yaml → mcp.python_exe 읽기
  3. fallback: ~/miniconda3/envs/intel_engram/python.exe

환경변수 주입:
  env["ENGRAM_DB_DIR"] = get_db_root_dir()
  env.pop("ENGRAM_RUNTIME_ROLE", None)  # KuzuDB 접근 허용 (overlay는 skip)
```

## MCP 서버 수명 모델 (Phase 3)

`mcp_server.py`는 overlay.exe가 시작할 때 **지속 subprocess**로 spawn되며,
overlay 종료 시 함께 종료됩니다. 모든 MCP 클라이언트는 SSE 트랜스포트로
동일 인스턴스에 연결합니다. 터미널을 닫아도 MCP 서버는 유지됩니다.

### 전체 런타임 흐름

```
overlay.exe (항상 실행)
  ├── overlay/main.py
  │     ├── tkinter 스프라이트 + pystray 트레이
  │     ├── mcp_server.py subprocess (port 17385 SSE) ← Phase 3 추가
  │     │     시작: _start_mcp_http_server()
  │     │     로그: ~/.engram/mcp-http.log
  │     │     종료: terminate() → wait(5s) → kill()
  │     ├── STMServer(port 17384) — HTTP 브로커 상주
  │     └── KG 파일 워처 데몬
  └── 캐릭터 클릭 → wt 창 열기 (ENGRAM_SCOPE_KEY=overlay 주입)

MCP 클라이언트들 (모두 SSE http://127.0.0.1:17385/sse):
  ├── Copilot CLI  → ~/.copilot/mcp-config.json  { type: sse }
  ├── Gemini CLI   → gemini mcp list              { type: sse }
  ├── Claude Code  → ~/.claude.json               { type: sse }
  ├── Goose        → ~/.config/goose/config.yaml  { type: sse }
  ├── VS Code ws   → .vscode/mcp.json             { type: sse }
  └── VS Code gbl  → %APPDATA%/Code/User/mcp.json { type: sse }
```

### frozen(overlay.exe) 환경에서의 Python 경로 탐색

```
_find_mcp_python() 우선순위:
  1. 비frozen 환경: sys.executable 그대로 사용
  2. frozen 환경: overlay.user.yaml → mcp.python_exe 읽기
  3. fallback: ~/miniconda3/envs/intel_engram/python.exe

환경변수 주입:
  env["ENGRAM_DB_DIR"] = get_db_root_dir()
  env.pop("ENGRAM_RUNTIME_ROLE", None)  # KuzuDB 접근 허용 (overlay는 skip)
```

### mcp_server.py 실행 인자

```bash
python mcp_server.py --transport sse --port 17385 --host 127.0.0.1
# 기존 stdio 모드 (Claude Code 자체 관리 시 — 현재 미사용):
python mcp_server.py  # 기본값 stdio
```

## STM 브로커 아키텍처

Phase 2에서 구현된 STM HTTP 브로커 서버로, `overlay.exe` 내에 상주하며
VS Code Copilot과 wt 터미널 간 STM(단기 기억) 공유를 가능하게 합니다.

### STM HTTP 브로커 엔드포인트 (`overlay/stm_server.py`, port 17384)

| 엔드포인트           | 메서드 | 설명                                         |
| -------------------- | ------ | -------------------------------------------- |
| `/health`            | GET    | `{ status: "ok", pid }` 헬스체크             |
| `/stm/session/start` | POST   | `{ session_id, scope_key }` 세션 시작        |
| `/stm/message`       | POST   | `{ status }` 메시지 저장 (request_id 멱등성) |
| `/stm/messages`      | GET    | `?scope_key=...` → `{ messages: [...] }`     |
| `/stm/session/close` | POST   | 세션 종료 + STM→LTM 승격 트리거              |

### STM 승격 흐름 (`core/stm_promoter.py`)

```
세션 종료 (POST /stm/session/close)
  → stm_promoter.promote()
    1. novelty 계산:
         fuzzy triangular membership (코사인 유사도 기반)
         + volume 가중치 (메시지 수)
         + recency 가중치 (최근 접근 시간)
    2. novelty 임계값 초과 → Ollama qwen2.5:1.5b 요약 생성
    3. 요약 → memories 테이블 (LTM) 승격
```

## CLI 백엔드 선택 (REPL / cli_bridge 이력)

### 과거 → 현재 → 미래

| 시기     | 방식                                                                                                | 상태                               |
| -------- | --------------------------------------------------------------------------------------------------- | ---------------------------------- |
| **과거** | `engram.py` REPL — `claude -p` subprocess 래핑                                                      | ~~Deprecated~~ (`sys.exit(1)`)     |
| **과거** | `core/cli_bridge.py` — Claude Code ↔ claude CLI 브릿지                                              | ~~Legacy~~ (`NotImplementedError`) |
| **현재** | `engram-copilot.cmd` → `copilot --model claude-sonnet-4.6 --additional-mcp-config @mcp-config.json` | **운영 중**                        |
| **미래** | install 옵션으로 Copilot CLI / Claude Code 선택 가능 (engram-copilot.cmd / install 스크립트 분기)   | 계획 중                            |

### 실제 Copilot CLI 진입점

```
~/.engram/engram-copilot.cmd  (install 스크립트가 생성)
  └── copilot --model claude-sonnet-4.6 --additional-mcp-config @mcp-config.json
```

`engram.py`는 **현재 실행 불가** (legacy stub) — 과거 설계 문서나 참조가 남아있어도 실제로는 동작하지 않음.

## 빌드 (`engram-overlay.spec`)

### PyInstaller hiddenimports 관리

`overlay.exe`는 PyInstaller로 패키징된다. `engram-overlay.spec`의 `hiddenimports` 목록을
**정확히 유지하는 것이 필수**다.

**왜 hiddenimports가 필요한가?**
`stm_server.py`의 core 모듈 임포트는 모두 함수 내부에서 지연(lazy) import된다:

```python
# stm_server.py — PyInstaller 정적 분석이 감지 못함
def _get_port():
    from core.runtime_config import get_cfg_value   # ← 지연 import
    ...

def do_POST(self):
    from core.memory_bus import memory_bus           # ← 지연 import
    from core.stm_promoter import maybe_promote      # ← 지연 import
```

PyInstaller는 실행 경로를 추적하지 않고 정적 분석만 하므로, 이 모듈들이
빌드에서 누락되면 **STM 서버가 묵묵히 실패**한다 (OSError가 아니라
`ImportError`를 조용히 삼키는 구조).

### 현재 hiddenimports 목록

| 모듈                   | 역할           | 사용 위치             |
| ---------------------- | -------------- | --------------------- |
| `core.context_builder` | 컨텍스트 조립  | backend.py            |
| `core.db`              | DB 연결        | 전반                  |
| `core.identity`        | 정체성         | mcp_server            |
| `core.memory`          | 기억 저장      | stm_server.do_POST    |
| `core.directives`      | 지침           | mcp_server            |
| `core.reflection`      | 반성           | mcp_server            |
| `core.curiosity`       | 궁금증         | mcp_server            |
| `core.sanitizer`       | 입력 정제      | mcp_server            |
| `core.memory_bus`      | 세션 파사드    | stm_server.do_POST    |
| `core.runtime_config`  | 설정 포트 읽기 | stm_server.\_get_port |
| `core.stm_promoter`    | STM→LTM 승격   | stm_server.do_POST    |
| `core.activity`        | 활동 로그      | mcp_server            |
| `core.project_scope`   | scope 해석     | mcp_server            |

### 증상 및 진단

```
증상: overlay.exe 실행 중이지만 STM 브로커를 인식 못함
    engram_status → { "stm_mode": "direct_sqlite", "broker_alive": false }

진단:
    1. netstat -ano | findstr ":17384"   → 응답 없으면 포트 미열림
    2. ~/.engram/overlay.log 확인        → STM 관련 로그 없으면 hiddenimports 문제
    3. exe 빌드 날짜 vs 소스 mtime 비교  → exe가 오래됐으면 재빌드 필요
```

### 재빌드 방법

```powershell
cd C:\Users\jhjang\vault623\workspace\projects\ProjectIntelContunuum

# 기존 overlay 종료 (트레이 우클릭 → 종료 또는)
# Get-Process engram-overlay | Stop-Process -Id <PID>

# 빌드
C:\Users\jhjang\miniconda3\envs\intel_engram\python.exe -m PyInstaller engram-overlay.spec --noconfirm

# 재시작
Start-Process dist\engram-overlay.exe

# 확인
Invoke-WebRequest -Uri "http://127.0.0.1:17384/health"
```

> ⚠️ **주의**: `core/`에 새 모듈을 추가하고 `stm_server.py`나 overlay 코드에서
> 지연 import로 사용하면 **반드시 spec의 hiddenimports에도 추가**해야 한다.

## 확장 계획

- **백그라운드 데몬**: Ollama 기반 자율 동작
- **KG 임베딩 청킹**: 200자 요약 한계 극복, 노트 전체 청킹
- **멀티 PC 동기화**: DB 레플리케이션

## 시각화 도구

### kg_viz.py — KG 그래프 HTML

`scripts/kg_viz.py`: pyvis 기반 인터랙티브 HTML 그래프 생성기.

```powershell
# KG 전체 그래프
python scripts/kg_viz.py

# memory DB 레이어 포함 (identity ★, memories ◆, directives ◆, curiosities ●)
python scripts/kg_viz.py --memory

# 특정 노드 중심 서브그래프 (BFS N홉)
python scripts/kg_viz.py --focus "Project_Engram" --hops 2 --memory

# 출력 경로 지정
python scripts/kg_viz.py --output D:\intel_engram\docs\my_graph.html
```

노드 표현:

- 크기 = in-degree (참조받는 수)
- 색상 = node type (concept/project/research/…) 또는 memory 타입
- hover = 제목 + 타입 + summary + 태그

### engram_dashboard.py — Streamlit 통합 대시보드

`scripts/engram_dashboard.py`: 브라우저 기반 통합 GUI.

```powershell
C:\Users\<username>\miniconda3\envs\intel_engram\Scripts\streamlit.exe run scripts/engram_dashboard.py
# → http://localhost:8501
```

| 페이지        | 내용                                                              |
| ------------- | ----------------------------------------------------------------- |
| 📊 Overview   | identity narrative, 테이블 통계, 최근 기억/지시문                 |
| 🕸️ KG Graph   | pyvis 그래프 — memory 레이어 토글, 시맨틱 엣지 토글 (임계값 조절) |
| 📝 Wiki Nodes | kg_nodes 테이블 브라우징, 클릭 시 vault 원문 + 연결 관계 표시     |
| 💭 Memories   | 에피소드 기억 전문                                                |
| 📋 Directives | 운영 지시문 목록 (비활성 포함 선택)                               |
| 🌐 Semantic   | 시맨틱 검색 + 노드별 유사 이웃 (KuzuDB 임베딩 기반)               |

의존 패키지: `streamlit`, `pandas`, `pyvis` (모두 `intel_engram` conda env에 설치됨)

## 전체 아키텍처

```
┌──────────────────────────────────────────────────────────────────┐
│                       인터페이스 계층                              │
│                                                                   │
│  ┌──────────────┐ ┌──────────────┐ ┌───────────────┐ ┌─────────────┐ │
│  │ engram-copilot│ │ Claude Code  │ │ Overlay       │ │  Discord    │ │
│  │ (Copilot CLI) │ │ (MCP 참조)   │ │ (GUI)         │ │             │ │
│  │               │ │               │ │ STMServer     │ │             │ │
│  │ 정체성 주체   │ │ 협력자        │ │ :17384 상주   │ │ 비동기 큐   │ │
│  └───────┬───────┘ └───────┬──────┘ └──────┬────────┘ └──────┬──────┘ │
│          │ MCP stdio       │ MCP stdio      │ HTTP/direct      │ SQLite │
└──────────┼─────────────────┼────────────────┼──────────────────┼────────┘
           │                 │                │                  │
           ▼                 ▼                ▼                  ▼
┌──────────────────────────────────────────────────────────────────┐
│                      mcp_server.py                                │
│                   FastMCP("engram") — 39 MCP Tools               │
│                                                                   │
│  ┌────────────┐ ┌──────────┐ ┌───────────┐ ┌──────────────────┐ │
│  │ Identity    │ │ Memory   │ │ Session   │ │ Directives       │ │
│  │ get_context │ │ search   │ │ start     │ │ add/list/update  │ │
│  │ get/update  │ │ save     │ │ save_msg  │ │ remove           │ │
│  │ narrative   │ │ list     │ │ reflect   │ ├──────────────────┤ │
│  │ persona     │ ├──────────┤ │ apply     │ │ Activity Log     │ │
│  │ themes      │ │ Curiosity│ └───────────┘ │ log/get          │ │
│  └────────────┘ │ add/list │               ├──────────────────┤ │
│                  │ address  │               │ Discord          │ │
│                  │ dismiss  │               │ read/send/mark   │ │
│                  └──────────┘               ├──────────────────┤ │
│                                             │ Consult Engram   │ │
│                                             │ (subprocess)     │ │
│                                             └──────────────────┘ │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│                       core/ 엔진 모듈                             │
│                                                                   │
│  identity.py ──── 자기 서술 + 페르소나 (EMA α=0.3)                │
│  memory.py ────── 3계층 기억 (단기/작업/장기) + 키워드 검색        │
│  reflection.py ── 세션 반성 + 정체성 진화                          │
│  curiosity.py ─── 호기심 큐 관리                                   │
│  directives.py ── 지침 CRUD + 스코프 렌더링                        │
│  activity.py ──── 외부 객체 활동 로그                              │
│  context_builder.py ── ~200토큰 압축 컨텍스트 조립                 │
│  runtime_config.py ─── YAML 기반 런타임 설정                       │
│  sanitizer.py ──── 입력 정제                                      │
│  db.py ────────── SQLite 스키마 + WAL + 마이그레이션               │
│  cli_bridge.py ── ~~claude CLI subprocess 래퍼~~ [Legacy: NotImplementedError] │
│  copilot_bridge.py ── Copilot CLI subprocess 래퍼                 │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│         <db.root_dir>\engram.db  (SQLite WAL)               │
│                                                                   │
│  identity ───── 1행: name, narrative, persona(JSON)               │
│  themes ─────── name(PK), weight, last_seen                       │
│  sessions ───── id, scope_key, started_at, ended_at, summary      │
│  messages ───── id, session_id, role, content, timestamp          │
│  memories ───── id, session_id, content, keywords, created_at     │
│  working_memory ─ scope_key(PK), summary, open_intents, expires   │
│  curiosities ── id, topic, reason, status, created/addressed      │
│  directives ─── key(PK), content, source, scope, priority, active │
│  activity_log ── id, actor, project, action, detail, created_at   │
│  discord_queue ─ id, guild_id, channel_id, content, processed     │
└──────────────────────────────────────────────────────────────────┘
```

## 멀티 객체 협력 모델

engram과 Claude Code는 동일한 DB를 공유하지만, 저작 주체가 구분됩니다:

|             | engram (Copilot CLI)                 | Claude Code                         |
| ----------- | ------------------------------------ | ----------------------------------- |
| 역할        | 정체성의 주체                        | 별개의 협력자                       |
| 기억 쓰기   | `save_memory` (1인칭)                | `log_activity` (3인칭)              |
| 정체성 변경 | `update_narrative`, `update_persona` | 읽기만                              |
| 반성        | `prepare/apply_reflection`           | 수행하지 않음                       |
| 상호 참조   | DB 직접 접근                         | `search_memories`, `consult_engram` |

```
engram가 반성할 때:
  1. 자신의 기억(memories) 되돌아봄
  2. 외부 활동 로그(activity_log) 참조 — "내가 부재한 사이 이런 일이 있었구나"
  3. 두 맥락을 통합하여 자기 서술 진화
```

## 데이터 흐름

### 세션 시작 (Copilot CLI 모드)

```
1. engram-copilot.cmd → COPILOT_CUSTOM_INSTRUCTIONS_DIRS=%USERPROFILE%\.engram 설정 → copilot 실행
2. Copilot CLI가 ~/.engram/copilot-instructions.md 읽음
3. LLM이 engram_get_context(user_query) 호출
4. context_builder가 ~200토큰 압축 텍스트 반환
5. LLM이 페르소나 채택 후 대화 시작
```

### 세션 시작 (Claude Code 모드)

<!-- cli_bridge.py는 과거 Claude Code ↔ claude CLI 브릿지였으나 현재 완전 미사용 (Legacy). -->
<!-- 현재 Claude Code는 MCP 프로토콜로 mcp_server.py에 직접 연결. -->

```
1. Claude Code 시작 → ~/.claude/CLAUDE.md 로드 (전역 지침)
2. ~/.claude.json에서 engram MCP 서버 자동 연결
3. CLAUDE.md 지침에 따라 engram_get_identity + get_themes 호출
4. engram의 축적된 경험을 참조하여 작업 수행
5. 의미 있는 작업 완료 시 engram_log_activity로 기록
```

### 반성 (Reflection)

```
사용자: "/reflect"
  → engram_prepare_reflection(session_id)
    → 대화 이력 + 정체성 + 페르소나 + 테마 수집
    → 외부 활동 로그(activity_log) 포함
  → LLM이 자기 반성 수행 + persona_observations 생성
  → engram_apply_reflection(...)
    → update_narrative()     # 서술 갱신
    → update_persona()       # EMA 블렌딩
    → decay_themes(0.95)     # 테마 감쇠
```

## 핵심 알고리즘

### 페르소나 EMA 블렌딩

```
new = current × 0.7 + observed × 0.3    # α=0.3
```

수치: 0~1 클램핑 · 리스트: 선두 삽입, 중복 제거, 최대 길이 제한

### 테마 감쇠

```
weight × 0.95 (세션 종료 시) · < 0.1 삭제
```

### 기억 3계층

| 계층 | 저장소                | 수명       | 용도                       |
| ---- | --------------------- | ---------- | -------------------------- |
| 단기 | messages 테이블       | 세션 내    | 최근 N턴 대화 컨텍스트     |
| 작업 | working_memory 테이블 | TTL 48시간 | scope별 요약 + 미완료 의도 |
| 장기 | memories 테이블       | 영구       | 에피소드 기억, 키워드 검색 |

### 컨텍스트 토큰 예산

```
~200 토큰 = 서술(~100) + 페르소나(~50) + 테마(~20) + 호기심(~20) + 프레임(~10)
```

## 모드 분리

|                 | `engram` (Copilot CLI) | `copilot` (기본) | Claude Code         | 독립 REPL          |
| --------------- | ---------------------- | ---------------- | ------------------- | ------------------ |
| 시스템 프롬프트 | 연속체 프로토콜        | 없음             | ~/.claude/CLAUDE.md | 연속체 프로토콜    |
| MCP             | engram (28 tools)      | 없음             | engram (28 tools)   | 없음 (직접 import) |
| 정체성          | engram (지속적)        | 매 세션 초기화   | 별개 객체           | engram (지속적)    |
| DB 쓰기         | 기억 + 정체성          | 없음             | 활동 로그만         | 기억 + 정체성      |

## Discord 연동 아키텍처

```
┌──────────────────────────────────────────────────────────────────┐
│                      Discord (외부)                               │
│  사용자 모바일 ──멘션──▶ Discord 봇                               │
└───────────────────────────┬──────────────────────────────────────┘
                            │ WebSocket 수신
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  discord_bot/bot.py  (EngramDiscordBot)                           │
│                                                                   │
│  on_message():                                                    │
│    1. allowed_user_ids 화이트리스트 검증                           │
│    2. [Discord/@username]: 내용 태그 부착 (Prompt Injection 방어) │
│    3. discord_queue 테이블에 INSERT (message_id 포함)             │
│    4. 원본 메시지에 🕐 리액션 추가                                │
│                          ↑ 토큰 소모 없음                         │
└───────────────────────────┬──────────────────────────────────────┘
                            │ SQLite INSERT
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│  engram (사용자 요청 시 처리)                                      │
│                                                                   │
│  1. engram_discord_read_queue()   → 미처리 메시지 조회         │
│  2. LLM 응답 생성 ← 토큰 소모 발생                               │
│  3. engram_discord_send(channel_id, content, message_id)       │
│     → Discord HTTP API로 전송 → 🕐 → ✅ 리액션 교체              │
│  4. engram_discord_mark_processed(id)                          │
└──────────────────────────────────────────────────────────────────┘
```

### 봇 생명주기

```
overlay.exe 시작
  → OverlayApp.__init__()
  → _try_start_discord_bot()
  → EngramDiscordBot.start()
  → discord.Client.start(token)  → Discord 봇 온라인
                    ↕ heartbeat (41초 주기, 무료)
트레이 우클릭 → 종료
  → OverlayApp.quit()
  → EngramDiscordBot.stop()
  → client.close()  → Discord 봇 즉시 오프라인
```

## 확장 계획

- **웹 GUI**: 이미지 인식/생성, 감정 스프라이트
- **백그라운드 데몬**: Ollama 기반 자율 동작
- **시맨틱 검색**: sentence-transformers 임베딩
- **멀티 PC 동기화**: DB 레플리케이션
