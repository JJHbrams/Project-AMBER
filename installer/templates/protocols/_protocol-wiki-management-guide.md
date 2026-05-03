---
id: wiki-management-guide
title: Wiki 작성 관리 규칙
note_type: protocol
tags:
  - wiki
  - guide
  - governance
created: __DATE__
updated: __DATE__
summary: engram wiki 작성·편집 시 준수할 상세 규칙. 구조·경로·Frontmatter·출처 명시·inbox 워크플로우 포함.
---

# Wiki 작성 관리 규칙

> 상세 지침 원본: [[wiki-guide]] (docs/guides/wiki-guide.md)

## 구조 변경 시

- 디렉토리 추가·삭제, 주요 프로젝트 추가 시 `docs/moc/000-HOME.md` 반드시 업데이트.

## 디렉토리 규칙

- 디렉토리명: 소문자 kebab-case (예: `projects/my-project/`, `research/llm/`)
- 같은 주제 노트 2개 이상이면 디렉토리로 묶을 것.

## 파일명 규칙

- 핵심 키워드만. timestamp 필요 시 파일명 앞에 `yymmdd_` 붙임.
- `kg_add_note` title 파라미터 = 파일명 슬러그 (한국어·공백 금지, kebab-case 영문)

## Frontmatter

- `note_type`(신규) 또는 `type`(legacy) 필수, `created`/`updated` 포함.

## KG 동기화

- kg_watcher가 자동 동기화.
- 이동·삭제·대규모 변경 후 → `kg_sync()` → `kg_lint()` 수동 실행.

## 출처 명시

| 상황                | 처리                           |
| ------------------- | ------------------------------ |
| 웹 페이지·논문 인용 | URL + 제목 + 날짜 각주로       |
| 출처 불명           | 기술 금지 또는 `(미확인)` 명시 |
| LLM 생성 내용       | `(LLM 추론, 검증 필요)` 명시   |

## \_inbox/ 워크플로우

- `_inbox/`: raw 검색·조사 결과를 임시 보관하는 버퍼.
- 해당 자료로 문서 작성 완료 후 반드시 정리(이동 또는 삭제).

## 관련 노트

- [[wiki-guide]]
