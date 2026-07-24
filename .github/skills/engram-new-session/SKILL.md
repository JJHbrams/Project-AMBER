---
name: engram-new-session
description: "engram 말풍선(bubble) 채팅의 상주 세션을 리셋해 새 대화를 시작한다. 트리거: 새 세션 시작, 새 대화 시작, 대화 초기화, 세션 리셋, /new-session, reset conversation, fresh session, start over. overlay 재시작 없이 현재 claude_session_id 를 비우고 상주 세션을 종료 → 다음 입력부터 이전 맥락 없는 새 세션."
argument-hint: "새 세션에서 처리할 첫 요청 (선택)"
---

# Engram 말풍선 — 새 세션 시작

overlay 의 말풍선 상주 세션은 같은 `claude_session_id` 로 계속 이어진다(overlay 재시작에도 resume).
사용자가 "새 세션/새 대화 시작/대화 초기화"를 원하면 아래로 **즉시 리셋**한다 (overlay 재시작 불필요).

## 실행 절차

1. overlay STM 서버 `/bubble/new` 로 POST:

   ```powershell
   Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:17384/bubble/new" -TimeoutSec 5
   ```
   (bash) `curl -s -X POST http://127.0.0.1:17384/bubble/new`

   포트 기본값은 17384 — 바꿨다면 `~/.engram/user.config.yaml` 의 `overlay.stm_server_port` 참조.

2. 응답이 `{"status":"ok","action":"bubble_new_session"}` 이면 성공. overlay 가 현재 상주 세션을
   종료하고 `claude_session_id` 를 비운다 → **다음 사용자 입력이 새 세션의 첫 메시지**가 된다.

3. 사용자에게 한 줄로만 알린다: "새 세션으로 시작할게 — 다음 메시지부터 이전 맥락 없이 이어져."

## 규칙

- 이 호출은 **현재 상주 세션을 종료**시킨다 — 호출 직후 현재 턴이 끊길 수 있고, 그게 정상이다.
- 연결이 실패하면(ConnectionRefused 등) overlay(engram-overlay.exe)가 실행 중이 아닌 것 —
  "overlay 가 실행 중이 아니라 새 세션을 시작할 수 없어" 한 줄로 안내한다.
- 인수가 있으면, 리셋 요청 후 그 인수를 새 세션의 첫 요청으로 이어서 처리한다.
