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
summary: 코딩·분석 작업 시작 전 관련 선행 기록을 wiki에서 확인하고 필요한 내용을 작업에 반영하는 절차.
---

# 작업 전 Wiki 참조 확인 절차

> 코딩/분석 작업 전, 유사한 선행 작업 기록이 있는지 확인한다.

## 절차

1. `kg_wiki_reminder(query=<작업 내용 한 줄 요약>)` 호출.
2. 관련 hit가 있으면 필요한 노드를 읽고 작업의 제약·선행 결정에 반영한다.
3. 기존 기록과 사용자 요청이 충돌하거나 작업 방향을 크게 바꿔야 할 때만 사용자에게 질문한다.
4. 단순 hit 존재 여부를 확인받기 위해 작업을 중단하지 않는다.

## 건너뛰는 경우

- 단순 질의응답·설명 요청 (코드 변경 없는 이해 요청)
- 파일·코드 탐색, 확인만 하는 작업
- 사용자가 참조 자료를 직접 첨부한 경우

## 실행 주체

- 저장소 변경 작업: `engram-task-workflow` skill
- Wiki 작성·수정: `engram-wiki-workflow` skill
- 이 문서 자체는 자동 실행기가 아니라 상세 정책의 SSOT다.
