---
name: engram-close-session
description: "Engram의 명시적 세션 종료·수동 반성을 수행한다. 트리거: 세션 종료, 대화 종료, 수고, 끝, /reflect, 반성, close session. 중간 메모리/Wiki 정리는 이 skill이 아니라 engram_summarize_session을 사용한다."
argument-hint: "다음 세션에 남길 open intent (선택)"
---

# Engram session close workflow

세션을 닫을 때 activity, curiosity, narrative, memory 저장을 한 번의 절차로 처리한다.

중간 메모리/Wiki 정리, 진행상황 저장 요청은 세션을 닫지 않는다. `engram_summarize_session`을 사용하고 이 skill을 실행하지 않는다.

## 실행 절차

1. 의미 있는 작업이 있었다면 `engram_log_activity`로 완료 내용을 기록한다.
2. `engram_list_curiosities(status="pending")`로 미해결 curiosity를 확인한다.
3. 사용자 피드백이나 `/reflect`가 있었으면 관련 curiosity와 이번 세션을 반성한다.
4. 정체성 변화가 있을 때만 `new_narrative`와 `persona_observations`를 작성한다. 변화가 없으면 둘 다 빈 값으로 확정한다.
5. 다음에 이어 할 일이 있으면 `open_intents`에 구체적으로 남긴다.
6. `engram_close_session`을 모든 파라미터와 함께 정확히 한 번 호출한다.
   - 오케스트레이터: `trigger_sync=True`
   - subagent: `trigger_sync=False`
7. 일부 저장 단계가 실패하면 성공으로 포장하지 말고 실패 항목을 사용자에게 알린다.

## 규칙

- `engram_close_session` 호출 전 curiosity 확인을 생략하지 않는다.
- 일상적인 작업 완료만으로 narrative를 갱신하지 않는다.
- watchdog placeholder나 자동 종료를 명시적 세션 종료로 간주하지 않는다.
