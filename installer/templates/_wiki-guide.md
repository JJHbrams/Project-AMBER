---
id: wiki-guide
title: Wiki 관리 지침
note_type: concept
tags:
  - guide
  - management
  - wiki
created: __DATE__
updated: __DATE__
---

# Wiki 관리 지침

> engram LLM wiki (docs/) 편집 시 반드시 따를 규칙.

---

## 1. MoC (HOME) 업데이트 규칙

- docs/moc/000-HOME.md 은 wiki 전체의 root 노드.
- 디렉토리 추가·삭제, 주요 프로젝트 추가 시 **반드시** HOME 파일을 업데이트할 것.
- HOME 수정 후 kg_sync() 또는 kg_update_node() 로 KG DB와 시맨틱 레이어 동기화.

---

## 2. 디렉토리 구조 규칙

- 최대 3계층 깊이
- 디렉토리명: **소문자 kebab-case** (예: `projects/karpathy/`, `research/medical-imaging/`)
- 새 디렉토리: `kg_add_note(note_type="projects/my-project")` 트릭 사용 후 frontmatter `type`을 `moc`으로 수정

| 디렉토리 | 용도 |
|----------|------|
| _inbox/ | 미분류 임시 노트 (fleeting 타입, 30일 내 이동) |
| _templates/ | 노트 템플릿 |
| _temp/ | 이동 stub, 30일 후 삭제 |

### note_type → 저장 디렉토리 매핑

| note_type | 저장 디렉토리 | 용도 |
|---|---|---|
| concept | concepts/ | 개념·기술 정리 |
| protocol | protocols/ | 운영 규칙·지침 |
| research | research/ | **외부** 논문·서비스 조사 결과 (프로젝트 내부 노트는 project 사용) |
| project | projects/ | 프로젝트 노트 (개발·설계·진행·회고) |
| tool | tools/ | 도구·라이브러리 레퍼런스 |
| reference | references/ | 외부 문서·링크 모음 |
| person | people/ | 인물 노트 |
| moc | moc/ | 지도 노트 (목차·인덱스) |
| fleeting | _inbox/ | 임시 메모 |

> ⚠️ 프로젝트 내부 개발/설계 노트 → `note_type="project"`, `projects/<프로젝트명>/` 하위 디렉토리 먼저 확인

---

## 3. 파일 규칙

- 파일명: **핵심 키워드만** — 의미 전달되는 최소한의 단어로 구성 (장황한 설명 금지)
  - 좋은 예: `karpathy-llm-wiki.md`, `stm-broker.md`, `fuzzy-drive.md`
  - 나쁜 예: `karpathy-llm-wiki-llm-기반-지식-베이스-패턴.md`
- timestamp 필요 시: **파일명 앞에** 붙임 — `yymmdd_파일명.md`
  - 예: `260422_karpathy-llm-wiki.md`
- 새 노트 작성 시: **기존 노트와 묶을 수 있으면 디렉토리로 구성**
  - 같은 주제·저자·프로젝트 노트가 2개 이상이면 디렉토리 생성 후 이동
  - 예: `research/karpathy/karpathy-llm-wiki.md`, `research/karpathy/karpathy-nanoGPT.md`
- frontmatter: `_templates/` 기준 준수
  - 신규 노트: `note_type:` 필드 사용
  - 기존 노트(legacy): `type:` 필드 허용 — lint 통과, 수정 시 `note_type:` 으로 교체 권장
- `kg_add_note`의 `title` 파라미터는 **파일명 슬러그**로 사용됨
  - 한국어·공백 금지, kebab-case 영문, 핵심 키워드 2~4개
  - 사람이 읽는 노트 제목은 본문 frontmatter `title:` 필드로 별도 지정
  - 좋은 예: `title="dashboard-visjs-graph-ui"` (frontmatter: `title: Dashboard vis.js Graph UI`)
- 관련 노트는 항상 [[위키링크]] 형식으로 연결

---

## 4. 노트 작성 포맷

빠른 참조 가능성 최우선. 권장 섹션 순서:
1. 제목 + > blockquote 한 줄 요약 (무조건 첫 줄)
2. 핵심 개념 표
3. 구조/아키텍처
4. 상세 내용
5. 트러블슈팅 (있을 경우)
6. 관련 노트 ([[]])

| 요소 | 규칙 |
|------|------|
| 파라미터/옵션 | 표 형식 (이름 \| 기본값 \| 설명) |
| CLI 명령어 | 표 형식 (명령어 \| 동작 \| 주요옵션) |
| 코드 | 언어 명시 (```python```, ```powershell``` 등) |
| 트리 구조 | ```ascii``` 코드 블록 |

피할 것: 긴 산문 나열, README 복붙, "이 노트는..." 식 접두사

---

## 5. 출처 명시 규칙

사실·수치·외부 정보를 기술할 때는 **반드시 출처를 명시**한다.

### 인라인 주석 (권장)

```markdown
LLM의 컨텍스트 창은 최대 200k 토큰이다.[^1]

[^1]: https://www.anthropic.com/claude — Claude 3 Technical Spec, 2024
```

또는 짧은 인라인 형식:

```markdown
GPT-4는 2023년 3월 공개되었다. (출처: [OpenAI blog](https://openai.com/gpt-4))
```

### 규칙 요약

| 상황 | 처리 |
|------|------|
| 웹 페이지·논문 인용 | URL + 제목 + 날짜(선택) 각주로 |
| 직접 실험·관측 결과 | `(직접 측정, yyyy-mm-dd)` |
| 출처 불명 | 기술 금지 또는 `(미확인)` 명시 |
| LLM 생성 내용 | `(LLM 추론, 검증 필요)` 명시 |

### 출처 섹션

노트 하단에 `## 참고` 섹션을 두고 모든 각주를 집약한다.

---

## 6. KG 동기화 체크리스트

노트 생성·수정·이동 후:
1. kg_sync() — vault .md → SQLite DB 동기화
2. kg_update_node(node_id, summary) — 프로젝트 노드 상태 갱신
3. HOME 파일 변경 시 → KG 노드 000-home summary도 업데이트
4. kg_lint() — 품질 점검 (고립 노드, _inbox 체류, summary 누락 등)

---

## 7. 조사→Wiki 저장 습관

**새 정보를 얻으면 반드시 wiki에 저장한다.**

| 상황 | 행동 |
|------|------|
| 빠른 메모·출처만 있는 경우 | _inbox/ 에 임시 노트 투기 |
| 충분히 정제된 경우 | 바로 적절한 디렉토리에 노트 생성 |
| 기존 노트와 같은 주제 | 기존 노트 업데이트 또는 하위 노트로 추가 |
| 저장 후 | kg_lint() 호출로 품질 점검 |

조사 결과를 채팅에만 남기고 wiki에 저장하지 않으면 다음 세션에서 잃어버린다.

---

## 8. _inbox/ 워크플로우

raw 검색·조사 결과를 임시 보관하는 버퍼. Karpathy LLM Wiki의 **raw/ 레이어**에 해당.

### 흐름

```
raw 검색·조사 결과 수집
    ↓
_inbox/제목.md 생성 (frontmatter 최소한만)
    ↓
해당 자료로 문서 작성 완료
    ↓
_inbox/ 원본 정리 (적절한 디렉토리로 이동 또는 삭제)
    ↓
kg_sync() → kg_lint()
```

### _inbox/ 노트 최소 frontmatter

```yaml
---
title: 간략한 제목
note_type: fleeting
tags: []
created: yyyy-mm-dd
---
```

- _inbox/ 노트는 30일 이내 정제·이동 원칙.
- kg_lint() 가 _inbox 체류 노트를 자동 감지한다.
