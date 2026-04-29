# Memory Tiering Change Notes (2026-04-05)

## 배경

기존 구조는 세션 단위 기록은 있었지만, 기본 모드와 Discord 모드에서 단발 호출이 잦아
"방금 대화"의 연속성이 약해질 수 있었다.

이번 변경의 목표는 다음과 같다.

1. 세션 수명과 메모리 수명을 분리한다.
2. 새 세션이 열려도 단기/임시/장기 메모리는 스코프 기준으로 이어지게 한다.
3. 기본 모드, Discord 모드, MCP 진입점이 같은 메모리 모델을 사용하도록 통일한다.

## 메모리 계층 정의

1. 단기 메모리 (Short-term)

- 스코프 범위에서 최근 메시지를 세션 경계를 넘어 재사용한다.
- 현재 기본 정책: 최근 8턴, 최근 120분 창.

2. 임시 메모리 (Working)

- 진행 중인 대화 흐름을 요약 문자열로 누적한다.
- 현재 기본 정책: TTL 48시간, 최대 길이 900자.

3. 장기 메모리 (Long-term)

- 기존 memories 테이블 기반 에피소드 기억 검색을 유지한다.
- 현재는 전역 풀을 사용하며, 이후 ontology/index schema를 통해 프로젝트 간 관계 추론을 강화할 예정이다.

## 설계 로드맵

- 장기 방향은 `docs/memory-ontology-roadmap.md`를 따른다.
- 핵심 원칙:
  1. SQLite를 canonical store로 유지
  2. index tables로 엔티티/관계를 정규화
  3. graph layer는 보조 탐색 계층으로만 도입
  4. 한 PC의 모든 기억 자산은 `~/.engram/user.config.yaml`의 `db.root_dir` 아래에서 일괄 관리

## 스키마 변경

### sessions 테이블

- `scope_key` 컬럼 추가 (NOT NULL, 기본값 `default`)
- 목적: 세션을 대화 범주(예: `default:main`, `discord:<channel_id>`)에 귀속

### working_memory 테이블 (신규)

- `scope_key` (PK)
- `summary`
- `open_intents`
- `updated_at`
- `expires_at`

### 인덱스

- `idx_sessions_scope_started` on sessions(scope_key, started_at)
- `idx_messages_session_ts` on messages(session_id, timestamp)
- `idx_working_memory_expires` on working_memory(expires_at)

### 마이그레이션 안정성

- 기존 DB에서도 `sessions.scope_key`가 없으면 자동 추가.
- 인덱스 생성 시점은 컬럼 마이그레이션 이후로 보정.

## 코드 변경 요약

## 1) core/storage/db.py

- sessions에 `scope_key` 추가
- working_memory 테이블 추가
- scope_key 마이그레이션 및 인덱스 생성 순서 보정

## 2) core/memory/store.py

- `create_session(scope_key)` 추가
- `get_recent_messages_by_scope(...)` 추가
- `get_working_memory(...)` 추가
- `upsert_working_memory(...)` 추가
- `append_working_memory_hint(...)` 추가

---

# 진입점별 STM 동작 현황 (2026-04-22)

## 현황

| 진입점 | 세션 생성 | 메시지 저장 | STM 동작 |
|--------|----------|------------|---------|
| `engram.py` REPL | ✅ `memory_bus.start_session()` | ✅ `record_user/assistant_message()` | ✅ 실제 쌓임 |
| `overlay/backend.py` | ❌ 없음 | ❌ 없음 | ❌ 매 호출 독립 실행 |
| MCP (Copilot/Claude) | LLM이 `engram_start_session` 호출 시 | LLM이 `engram_save_message` 호출 시 | LLM 의존 (불안정) |

## Overlay 갭 원인

`overlay/backend.py`는 `engram.cmd -p "..."` 방식의 subprocess single-shot 호출이라
매 질문마다 새 Copilot 프로세스가 생성됨. Python 측에서 세션을 생성하지 않으므로
messages 테이블에 턴 데이터가 쌓이지 않음.

## 해결 방향

`EngramBackend.__init__`에서 `memory_bus.start_session(scope_key="overlay")` 호출,
`_run()` 내부에서 `record_user/assistant_message()` 직접 호출.

- 추가 LLM 토큰: 0 (Python에서 SQLite write만 발생)
- scope_key 정책: `"overlay"` 고정(분리) 또는 `ENGRAM_WORKDIR` 기반(REPL과 통합)
  → 기본값은 분리, 추후 결정
- 구현 예정: `overlay/backend.py` 수정
- `save_memory`가 `session_id=None`도 허용하도록 확장

## 3) core/context/context_builder.py

- `build_system_prompt(..., scope_key="")`로 확장
- 컨텍스트 합성 순서:
  1. directives
  2. short_term
  3. working_memory
  4. long-term memories
  5. curiosity

## 3.5) core/memory/bus.py

- 기존 `memory.py` / `context_builder.py` 헬퍼 위에 얇은 orchestration 레이어 추가
- 담당 범위:
  - 스코프 기반 세션 시작
  - user / assistant 메시지 기록
  - 세션 최근 대화 조회
  - prompt context 조립 위임
  - assistant 응답 후 working memory 갱신
  - REPL용 에피소드 기억 저장 cadence helper
- 명시적 scope가 없으면 현재 프로젝트 경로에서 project scope를 자동 파생하고, 프로젝트를 판별할 수 없으면 global scope로 폴백

## 4) engram.py (기본 REPL)

- 기본 스코프: 현재 프로젝트에서 자동 파생되는 `project:<slug>-<hash>` (없으면 `global:main`)
- 메모리 관련 orchestration을 `core.memory.bus`로 일원화
- 새 세션 생성은 유지하되, 스코프 기반으로 생성
- 매 턴 응답 후 working memory 갱신
- system prompt 조립 시 scope_key 전달

## 5) discord_bot/bot.py

- 채널별 스코프 사용: `discord:<channel_id>`
- 메시지마다 새 세션 생성 후 user/assistant 메시지 저장
- system prompt 조립 시 채널 스코프 전달
- 응답 후 working memory 갱신
- 메모리 orchestration은 `core.memory.bus`를 통해 수행
- 봇 시작 시 DB 초기화 보장

## 6) mcp_server.py

- `engram_get_context`는 `core.memory.bus`를 통해 context를 조립
- `engram_get_context` / `engram_start_session`은 `scope_key` 또는 `project_key`를 받을 수 있음
- 둘 다 비어 있으면 MCP 서버의 현재 프로젝트에서 scope를 자동 파생
- long-term memory는 전역 풀을 유지하고, short-term / working memory만 project scope를 따름

## 7) 정체성 주입 문서/스킬

- `.github/copilot-instructions.md`의 세션 시작 호출을
  `engram_get_context(scope_key="default:main")`로 변경
- `.github/skills/engram/SKILL.md`도 동일 스코프로 통일

## 런타임 동작

## 기본 모드 (engram.py)

1. 실행 시 새 session row 생성 (scope_key=`default:main`)
2. 매 턴 user/assistant 메시지를 messages에 저장
3. prompt 조립 시
   - short_term: 최근 scope 메시지
   - working_memory: 최근 누적 요약
   - long-term: memory search
4. 응답 후 working_memory를 갱신

## Discord 모드

1. 멘션 수신 시 새 session row 생성 (scope_key=`discord:<channel_id>`)
2. user/assistant 메시지를 messages에 저장
3. prompt 조립 시 동일하게 short_term + working + long-term 주입
4. 채널 단위로 연속성 유지

## 배포/적용

변경 반영 후 다음 명령으로 DB 마이그레이션과 스킬 배포를 적용한다.

`./scripts/install.ps1`

## 현재 제약

1. working memory는 현재 문자열 누적 방식이며 요약 모델 기반 압축은 아직 미적용.
2. MCP 경유 일반 대화에서 자동 working-memory 갱신 루프는 별도 구현 여지 있음.
3. TTL, 길이 제한은 코드 상수이며 설정 파일 외부화는 후속 과제.

## 검증 상태

1. 변경 파일 정적 오류 검사: No errors
2. install.ps1 재실행 시 DB 초기화 정상
3. 기존 DB에서 scope_key 마이그레이션 경로 정상

## 변경 파일 목록

- .github/copilot-instructions.md
- .github/skills/engram/SKILL.md
- engram.py
- core/context_builder.py
- core/db.py
- core/memory.py
- core/memory_bus.py
- discord_bot/bot.py
- mcp_server.py
