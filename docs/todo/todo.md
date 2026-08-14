---
title: 작업 Todo
tags:
  - todo
  - tracking
created: 2026-04-28
updated: 2026-05-01
source: User Todo list.md
---

# 작업 Todo

> `User Todo list.md` 기반으로 자동 갱신. 완료 항목은 완료일로부터 7일 후 정리.

---

## ✅ 완료

| 항목                                                                                | 완료일     | 정리 예정  |
| ----------------------------------------------------------------------------------- | ---------- | ---------- |
| INSTALLER — directives template 누락 추가 (protocol 가이드 7종 → `docs/protocols/`) | 2026-04-28 | 2026-05-05 |
| Setting — 전역 설정 탭 추가 (CLI 자동 컨텍스트 주입, 재부팅 자동 실행)              | 2026-04-28 | 2026-05-05 |
| INSTALLER — 최초 이름 설정 시 돌이킬 수 없음 경고 안내 박스 추가                    | 2026-04-28 | 2026-05-05 |
| Setting — 전역 탭 이름 편집 필드 제거 (설치 시 단 1회 설정 방식으로 정책 변경)      | 2026-04-28 | 2026-05-05 |

---

## 🔲 미결

### core 리팩토링

- [x] `core/` 기능 단위 분류
- [x] 장문 단일 모듈 패키지화
  - [x] 리팩토링 원칙 확정 (shim/하위호환 레이어 배제, import 전면 교체)
  - [x] 목표 패키지 구조 합의
    - [x] `core/config/` (`runtime_config.py`)
    - [x] `core/storage/` (`db.py`)
    - [x] `core/identity/` (`identity.py`, `curiosity.py`, `reflection.py`)
    - [x] `core/memory/` (`memory.py`, `memory_bus.py`)
    - [x] `core/graph/knowledge/` (`knowledge_graph.py`)
    - [x] `core/graph/semantic/` (`semantic_graph.py`, `stm_promoter.py`)
    - [x] `core/context/` (`context_builder.py`, `directives.py`, `project_scope.py`)
    - [x] `core/integrations/` (`copilot_bridge.py`)
    - [x] `core/observability/` (`activity.py`, `call_log.py`)
    - [x] `core/dashboard/` (`dashboard.py` 및 page/render/data 모듈)
      - [x] `core/dashboard/app.py` + `core/dashboard/assets/*`로 본체/에셋 이동
      - [x] `pages/*`, `graph_render.py`, `semantic_api.py`, `data_access.py` 분해
    - [x] `core/common/` (`sanitizer.py` 등 공통 유틸)
  - [x] Phase 1 — 기반 모듈 이동
    - [x] config/storage/common 먼저 이동 후 전체 import 즉시 치환
  - [x] Phase 2 — 도메인 패키지 분해
    - [x] identity/memory 기능별 내부 모듈 분해
    - [x] context/observability/integrations 패키지 분리 + import 치환
  - [x] Phase 3 — graph 계층 분해
    - [x] knowledge/semantic 하위 패키지로 분리 + 동기화/검색/링킹 책임 분리
  - [x] Phase 4 — dashboard 분해
    - [x] `app.py`, `pages/*`, `graph_render.py`, `semantic_api.py`, `data_access.py` 분해
  - [x] Phase 5 — 정리
    - [x] 구 파일 삭제 + dead import 제거
    - [x] import 정렬/포맷 마무리
  - [x] 회귀 검증
    - [x] `python -m test.test_runtime_config`
    - [x] `python -m test.test_project_scope`
    - [x] `python -m test.test_memory_bus`
    - [x] `python mcp_server.py` 기동 확인
    - [x] `python overlay/main.py` 핵심 경로 확인
    - [x] `streamlit run scripts/engram_dashboard.py` 렌더 확인

### 튜토리얼

- [x] 최초 설치 시 단계별 튜토리얼 제공
  - [x] 튜토리얼 상태 스키마 확정 (`tutorial.version`, `state.current_step`, `state.completed_steps`, `continuity_test_enabled`)
  - [x] 튜토리얼 상태 저장 분리 (`~/.engram/tutorial.user.yaml`, legacy `user.config.yaml.tutorial` 자동 마이그레이션/정리)
  - [x] 단계별 완료 조건 정의
    - [x] 1단계 페르소나 설정: 이름 단계 제거 후 페르소나 설정을 시작 단계로 고정
    - [x] 2단계 위키 기초 튜토리얼
      - [x] 필수: "llm wiki 에 대해 조사하고 정리해줘" 사용자 직접 입력 유도 + 위키 절대경로 안내
      - [x] 완료 검증(3중): 산출물 노드 존재 + 사용자 확인 체크 + 이해 요약(최소 길이)
    - [x] 3단계 위키 심화 튜토리얼
      - [x] engram 프로젝트 기준 위키 구성 지시 실습(핵심 개념/프로젝트 요약/링크 정리)
      - [x] 심화 검증(3중): 프로젝트 위키 노드+링크 존재 + 사용자 확인 체크 + 지시 요약(최소 길이)
    - [x] 4단계 세션 연속성 튜토리얼
      - [x] 1차: 현재 세션 요약/메모리 저장 (`engram_close_session`)
      - [x] 2차: 새 세션에서 회상 질문으로 연속성 확인
      - [x] 동일 세션 즉시 완료 차단 + 새 세션 확인 후 최종 완료
      - [x] 목표: 세션 메모리화 습관 체득
      - [x] 연속성 검증(3중): memory 검색 히트 + 사용자 확인 체크 + 연속성 요약(최소 길이)
  - [x] 중단/재개/스킵 정책 정의 (skip 후 재개 지점 포함)
  - [x] 설정 > 전역 탭: `튜토리얼 플래그 초기화` 버튼 추가 (메모리/위키 유지, 진행 플래그만 초기화)
  - [x] MCP 도구: `engram_get_tutorial_status`, `engram_complete_tutorial_step` 추가
  - [x] MCP 도구: `engram_skip_tutorial_step`, `engram_resume_tutorial_step` 추가
  - [x] 3/4단계는 `진입 안내(목적 설명)+진행/보류 선택` 후, 진행 선택 시 `engram_proceed_tutorial_step`으로 실습 모드 전환
  - [x] `engram_get_tutorial_status`에 코드 기반 런타임 페이로드(`runtime.mode`, `runtime.prompt_to_user`, `runtime.choices`) 추가
  - [x] 3단계 기초(`wiki_basic`)는 선택지 대신 사용자 직접 입력 대기 모드(`runtime.mode=input`)로 고정
  - [x] 2/3단계 decision 모드에 고정 선택 질문(`choice_question`) 추가 + 사용자 선택 전 자동 실행 금지 규칙 강화
  - [x] MCP 도구: `engram_verify_tutorial_wiki_basic` 추가 (3단계 필수 검증)
  - [x] MCP 도구: `engram_verify_tutorial_wiki_advanced`, `engram_verify_tutorial_session_continuity` 추가
  - [x] 디버그 키워드 우회는 현재 단계에만 적용되도록 제한 + 정상 안내 흐름 유지
  - [x] 튜토리얼 종료 시 권장 습관 체크리스트 고정 출력 (`engram_get_context` 안내에 포함)

### Setting 개선

- [x] 전역 설정 — CLI 공급자 시작 시 자동 오버라이드 여부 (context 소모 경고 포함)
- [x] 재부팅 시 자동 실행 여부

### Discord 봇

- [x] 기본은 동일 채널에서 동일 세션 지속 사용
- [x] 사용자가 명시적으로 "새 세션" 요청 시 새 세션 생성 후 이어서 사용 (2026-05-01 적용)
- [ ] 세션 연속성 고도화(장기 세션 context rot 완화, 롤오버/compact 정책) — 보류 (사람마다 불편할 수 있어 기본 OFF/옵션 기능으로만 검토)
- [x] 입력 큐처리(채널별 순차 + 채널 간 병렬)
  - [x] 채널별 FIFO 보장 (동일 채널 메시지 순서 보존)
  - [x] 채널 간 병렬 처리 + 전역 동시성 제한(예: worker/semaphore)
  - [x] 큐 적재 한도/TTL/드롭 정책 정의 (과부하 시 최신 우선 또는 사용자별 병합)
  - [x] 큐 상태 가시화 (대기 건수, 평균 대기 시간, 실패/드롭 카운트)
- [x] 다중 채널/그룹(guild) 라우팅 config 확장
  - [x] 단일 `guild_id`, `channel_id`에서 `guild_ids`, `channel_ids` 배열 지원
  - [x] 그룹(guild)·채널 단위 allowlist/denylist 규칙 지원
  - [x] 라우팅 단위별 scope_key override 규칙 정의 (provider override 완료)
    - [x] provider override: 기본값은 마지막 overlay provider, 채널/길드 사전 지정값 우선
    - [x] scope_key override: 라우팅 단위 세션 분리 규칙 추가 (channel > guild > template > default)
  - [x] 기존 단일값 설정과의 하위호환/자동 마이그레이션

### Installer — Wiki 템플릿 정합 (2026-05-03 vault 대정리 기준)

> vault 구조 최적화 이후 installer 생성 템플릿과의 불일치 해소. 신규 설치 시 올바른 구조 보장.

- [x] `06_db.ps1`: `$WikiGuide` 경로 한국어 파일명 → `wiki-guide.md` (kebab-case) 수정
- [x] `06_db.ps1`: `$WikiDirs`에 `research/llm/`, `research/knowledge-systems/`, `research/agent/`, `research/cost/` 추가
- [x] `installer/templates/_home.md`: `protocols/` 폴더 행 추가 + wiki-guide 참조로 수정
- [x] `installer/templates/_wiki-guide.md`: `id: wiki-guide` 필드 추가, `note_type → 디렉토리 매핑` 표 추가 (protocol/fleeting 타입 포함)
- [x] `installer/templates/protocols/_protocol-wiki-management-guide.md`: `[[Wiki 관리 지침]]` → `[[wiki-guide]]` 참조 수정

---

### Directives 구조 개선 (Context Rot + Broken Reference 해소)

> wiki 확인 지침이 실제로 지켜지지 않는 원인 4가지 해결 계획. (분석: 2026-05-03 세션)

- [x] **Broken reference 및 중복 directive 정리**
  - [x] canonical 문서를 `wiki-guide`로 통일
  - [x] `wiki-management`, `wiki-reminder-on-task`, reflection 중복 directive 제거
  - [x] `wiki-governance-trigger`를 `wiki-workflow-dispatch`로 명확화
- [x] **조건부 workflow 보장**
  - [x] 기본 directive를 세션 시작 시 항상 주입되는 짧은 정책으로 전환
  - [x] Wiki 작성 절차를 `engram-wiki-workflow` skill로 승급
  - [x] 세션 종료·반성 절차를 `engram-close-session` skill로 승급
  - [x] installer가 Copilot/Claude 사용자 skill 경로에 설치하도록 연결
- [x] **기존 설치 DB 마이그레이션**
  - [x] installer 관리 기본값만 갱신하고 사용자 수정 directive는 보존

---

### LLM Harnessing 개선 (engram-llm-harnessing-gap-analysis 기반)

> engram × LLM Harnessing 갭 분석에서 도출한 단계별 개선 로드맵. (2026-05-03 세션)

- [ ] **Phase 1 — 데이터 스키마 변경** (난이도 낮음)
  - [ ] `memories` 테이블 컬럼 추가: `importance_score REAL DEFAULT 0.5`, `access_count INT DEFAULT 0`, `last_accessed TEXT`
  - [ ] 검색 쿼리 개선: 최신성 + 중요도 + 접근빈도 복합 점수 적용
  - [ ] `working_memory` 가변 TTL: KG 프로젝트 노드 연결 scope → `expires_at = None`
  - [ ] `memories` 테이블 `confidence REAL DEFAULT 1.0` 추가 (LLM 추론 0.7, 직접 관측 1.0)
- [ ] **Phase 2 — 컨텍스트 최적화**
  - [ ] `build_system_prompt` 섹션별 토큰 예산 비율 파라미터 추가
    - identity 5% / directives 10% / state 15% / kg_wiki 25% / episodic 20% / stm 25%
  - [ ] Directive 재주입 메커니즘: N턴마다 핵심 지시 재삽입 (context rot 완화)
- [ ] **Phase 3 — KG 자동 구축** (난이도 높음, 장기)
  - [ ] `engram_save_memory` 호출 시 백그라운드 엔티티 추출 → KG 자동 링크 파이프라인
  - [ ] 에이전트 역할 명시 (`directives` 테이블 `agent_role TEXT` 컬럼 추가)

---

### UI/UX

- [ ] 이미지 입력 기능
  - [ ] 이미지 파일 첨부
  - [ ] 클립보드 이미지 붙여넣기

### 메모리

- [ ] 오래된 메모리 가소성 (필요 여부 검토)
- [ ] 최신 내용과 위배되는 노드 정리
- [ ] 노드 증가 시 검색 품질 저하 여부 조사
- [x] 유휴 세션 자동 메모리 체크포인트
  - [x] 30분 유휴 + 마지막 갱신 이후 사용자 발화 5회 gate
  - [x] working memory 상시 갱신 + novelty 통과 시 LTM 저장
  - [x] activity log, Engram daily, vault623 daily, 프로젝트 Progress 상호 연결
