# Memory Ontology Roadmap

## 목표

Engram의 장기 기억 구조를 단순 문자열/키워드 중심 저장에서 벗어나,
**SQLite를 canonical store로 유지하면서 index tables와 가벼운 graph 탐색 계층을 얹는 온톨로지 시스템**으로 발전시킨다.

이 설계의 목적은 다음과 같다.

1. 프로젝트별 단기/작업 메모리와 전역 장기 기억을 함께 운영한다.
2. 여러 프로젝트에서 축적된 개념, 파일, 태스크, 인물, 이슈를 동일 엔티티로 재인식한다.
3. "이전 프로젝트의 어떤 경험이 현재 문제와 연결되는가"를 관계 기반으로 추적한다.
4. graphDB를 도입하더라도 SQLite를 단일 진실 원천(source of truth)으로 유지한다.

## 운영 원칙

### 1. 단일 PC 단위 DB 루트

- 한 PC에서 Engram의 모든 DB/문서/인덱스 자산은 **사용자 설정 파일 `~/.engram/user.config.yaml`의 `db.root_dir` 아래에서 일괄 관리**한다.
- 기본 DB 파일은 `<db.root_dir>/engram.db` 이다.
- 이후 graph cache, ontology snapshot, relation export 등을 추가하더라도 같은 root 아래에서 관리한다.
- 프로젝트별로 DB 파일을 따로 흩뿌리지 않고, 하나의 PC 단위 기억 루트에 축적한다.

예시:

```yaml
db:
  root_dir: "D:/intel_engram"
```

### 2. 저장소 계층 분리

- **SQLite**: canonical store, 트랜잭션, 스키마 마이그레이션, durable state
- **Index tables**: 엔티티/관계/프로젝트/메모리 연결의 정규화 계층
- **Graph layer**: 연관성 탐색과 경로 기반 회상을 위한 보조 조회 계층

즉, graphDB는 주 저장소가 아니라 **탐색 가속기 / 관계 추론 보조 계층**이다.

## 목표 구조

### A. 현재 구조

- `sessions`, `messages`, `working_memory`, `memories`
- `scope_key` 기반 STM / working 분리
- long-term memory는 전역 풀 검색

### B. 다음 구조

SQLite 내부에 다음 index schema를 추가한다.

| 테이블 | 역할 |
|---|---|
| `projects` | 프로젝트 정규화 (`project_key`, 루트 경로, 표시 이름) |
| `entities` | 개념/파일/인물/이슈/태스크 등의 엔티티 정규화 |
| `entity_aliases` | 동일 엔티티의 이름 변형, 별칭, 경로 변형 |
| `relations` | 엔티티 간 관계 (`depends_on`, `mentions`, `implements`, `same_as`) |
| `memory_entity_links` | 특정 memory/session/message 와 엔티티 연결 |
| `project_entity_links` | 프로젝트와 엔티티 연결 |
| `memory_promotions` | 어떤 로컬 기억이 왜 전역 기억으로 승격되었는지 추적 |

이 구조를 먼저 SQLite 조인으로 운용하고, 필요 시 graph representation을 병행한다.

## Milestones

### Milestone 1 — Scope-aware memory

- project scope / global scope 분리
- STM / working memory를 프로젝트 범위로 분리
- long-term memory는 전역 유지

상태: 구현됨

### Milestone 2 — Promotion rules

- project-local memory 중 어떤 정보를 global memory로 올릴지 규칙화
- 예: 재등장 빈도, 중요도, 사용자가 명시적으로 강조한 사실, 관계 밀도
- 승격 이력을 `memory_promotions`로 추적

### Milestone 3 — Ontology-friendly index schema

- `projects`, `entities`, `relations`, `memory_entity_links` 도입
- 최소한의 엔티티 추출/정규화 파이프라인 추가
- SQLite 인덱스와 조인만으로 1차 연관 탐색 가능하게 구성

### Milestone 4 — Graph-assisted recall

- SQLite의 index tables를 graph view로 투영
- 경로 기반 회상: "현재 문제와 연결된 과거 프로젝트/엔티티/해결 패턴"
- graph cache 또는 graphDB는 선택적 보조 계층으로 유지

### Milestone 5 — Retrieval policy tuning

- 키워드 검색 + typed relation recall + graph neighborhood recall 결합
- caller/context에 따라 retrieval budget 조절
- 과도한 전역 오염 없이 cross-project transfer를 강화

## 왜 지금 graphDB를 바로 넣지 않는가

지금은 ontology가 안정되기 전 단계라서, graphDB를 먼저 넣으면 다음 비용이 커진다.

1. 스키마 변경 비용
2. 동기화 복잡도
3. 운영 복잡도
4. 작은 데이터셋 대비 과도한 시스템 무게

따라서 현재 단계의 원칙은:

1. SQLite schema를 ontology-friendly 하게 확장한다.
2. index tables로 충분히 운용해 본다.
3. 실제 recall 품질 병목이 드러날 때 graph layer를 붙인다.

## 성공 기준

이 로드맵이 성공하면 Engram은 다음을 자연스럽게 수행할 수 있어야 한다.

1. 서로 다른 프로젝트의 같은 개념을 하나의 엔티티로 인식한다.
2. 현재 문제와 연결된 과거 프로젝트의 관련 태스크/파일/해결 패턴을 끌어온다.
3. 프로젝트 로컬 잡음은 유지하되, 진짜 재사용 가치가 있는 기억만 전역으로 승격한다.
4. 모든 기억 자산은 한 PC의 `db.root_dir` 아래에서 일관되게 관리된다.
