---
id: wiki-reminder-guide
title: 작업 전 Wiki 참조 확인 절차
note_type: concept
tags:
  - workflow
  - wiki
  - task
created: __DATE__
updated: __DATE__
summary: 코딩·분석 작업 시작 전 관련 선행 기록을 wiki에서 확인하는 절차. score≥0.45 hit 시 사용자에게 참조 제안.
---

# 작업 전 Wiki 참조 확인 절차

> 코딩/분석 작업 전, 유사한 선행 작업 기록이 있는지 확인한다.

## 절차

1. `kg_wiki_reminder(query=<작업 내용 한 줄 요약>)` 호출.
2. 결과에 score ≥ 0.45인 hit가 있으면 작업 진행 전 사용자에게 제안:

   ```
   이전에 유사한 작업을 수행한 기록이 있습니다:
   - [문서 제목](vault 경로 또는 노드 ID) — <요약 한 줄>
   해당 내용을 참조하여 작업할까요?
   ```

   - vault 경로: `kg_wiki_reminder` 결과의 `node_id` 또는 `source_path` 사용.
   - 경로 없으면 노드 제목만 표시.

3. 사용자가 승인 → 해당 내용 참조 후 작업.
4. 사용자가 거절 또는 hit 없음 → 즉시 작업 착수.

## 건너뛰는 경우

- 단순 질의응답·설명 요청 (코드 변경 없는 이해 요청)
- 파일·코드 탐색, 확인만 하는 작업
- 사용자가 참조 자료를 직접 첨부한 경우
