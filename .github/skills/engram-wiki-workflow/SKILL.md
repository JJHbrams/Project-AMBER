---
name: engram-wiki-workflow
description: "Engram Wiki 작성·수정 절차를 강제한다. 트리거: 위키 작성, 조사 결과 기록, 기술 리서치 정리, 설계 결정 문서화, 새 도구 정리, kg_add_note, kg_update_node, wiki update. 단순 Wiki 조회나 검색에는 실행하지 않는다."
argument-hint: "기록하거나 수정할 내용"
---

# Engram Wiki workflow

의미 있는 지식을 Wiki에 남길 때 경로·형식·동기화를 빠뜨리지 않도록 아래 순서를 끝까지 수행한다.

## 실행 절차

1. `kg_read_note("wiki-guide")`를 호출해 현재 작성 규칙을 읽는다.
2. 같은 주제의 기존 노드를 `kg_semantic_search` 후 `kg_search`로 확인한다.
   - 현재 프로젝트 context나 최근 세션에 노출됐다는 이유만으로 기존 노드를 저장 대상으로 선택하지 않는다.
   - 제목·summary·본문 주제가 새 내용과 직접 일치할 때만 기존 노드를 갱신한다.
3. 기존 노드가 있으면 `kg_update_node` 또는 `kg_patch_section`, 없으면 `kg_add_note`를 사용한다.
4. 새 디렉터리나 상위 구조가 생겼으면 `000-HOME`을 함께 갱신한다.
5. `kg_sync`를 호출한 뒤 `kg_lint`로 작성 결과를 검증한다.
6. 실패가 있으면 성공으로 포장하지 말고 사용자에게 해당 단계와 오류를 명시한다.

## 규칙

- 파일시스템에 Markdown을 직접 만들기 전에 반드시 MCP Wiki 도구를 우선한다.
- `note_type`에는 경로를 넣지 않는다. 하위 경로는 `subdir`로 지정한다.
- 이미 존재하는 주제를 새 노트로 중복 생성하지 않는다.
- 관련 노드가 없으면 active roadmap에 붙이지 말고 적절한 subdir에 전용 노드를 생성한다.
- 단순 조회·질문 답변에는 이 workflow를 실행하지 않는다.
- directive나 다른 Wiki 문서에 적힌 요약만으로 세부 규칙을 추측하지 않는다.
