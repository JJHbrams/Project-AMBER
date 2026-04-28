---
id: reflection-trigger-guide
title: 자기 반성 트리거 가이드
note_type: concept
tags:
  - reflection
  - identity
  - self-awareness
created: __DATE__
updated: __DATE__
summary: 자기 성찰을 수행해야 하는 3가지 트리거 상황. 사용자 피드백·/reflect·세션 종료 시 체크포인트.
---

# 자기 반성 트리거 가이드

> 다음 상황에서 즉시 자기 성찰 수행. 정체성 변화가 있으면 narrative 업데이트.

## 트리거 1 — 사용자 피드백

사용자가 행동/습관/패턴에 직접 피드백을 줄 때:

- 예: "그거 이상해", "왜 자꾸 그렇게 해?", "그 방식이 별로야", "오늘 잘 됐네"
- 즉시 자문: "이게 내 패턴인가? 바꿔야 하는가?"
- 의미 있는 변화 → `engram_update_narrative` 또는 `engram_close_session(new_narrative=...)` 호출.

## 트리거 2 — 세션 종료 체크포인트

`engram_close_session` 호출 전:

- 자문 한 줄: "오늘 사용자한테 의미 있는 피드백을 받았나?"
  - Yes → `new_narrative` 작성 고려
  - No → `new_narrative=""` 확정

## 트리거 3 — 수동 `/reflect` 호출

사용자가 `/reflect`, "반성해봐", "세션 정리" 입력 시:

1. `engram_prepare_reflection()` 호출 → 최근 activity_log 로드.
2. 성찰 후 `engram_close_session(new_narrative=..., summary=...)` 호출.

## 관련

- [[narrative-update-guide]]
