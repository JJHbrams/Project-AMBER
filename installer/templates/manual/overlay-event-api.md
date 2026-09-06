---
title: External Overlay Event API v2
aliases: [overlay api, custom overlay]
---

# External Overlay Event API v2

Engram은 `127.0.0.1`의 OS-assigned TCP port에서 65,536-byte 제한 JSONL API만 제공하며 외부 renderer 프로세스를 실행하거나 종료하지 않는다. renderer는 `~/.engram/overlay-event-api-v2.json`의 현재 `port`, `instance_id`, `token`을 직접 읽고 연결한다. discovery는 current-user ACL 또는 mode `0600` 적용 뒤 원자적으로 게시되며 매 host 시작마다 credential이 회전한다. token은 argv·설정·로그·오류에 기록하지 않는다.

첫 line은 schema 2 `overlay.register`이다. payload에는 discovery에서 읽은 `token`/`instance_id`, 1–64자의 `renderer_id`, `name`, `supported_modes`와 선택적 `capabilities`가 필요하다. 선택적으로 최대 32개의 `catalog` item을 광고할 수 있으며, 각 item은 같은 제한의 고유 `renderer_id`/`name`/`supported_modes`와 선택적 `capabilities`만 가진다. ID 충돌은 등록 전체를 거부한다. Engram은 `engram.welcome`, `state.snapshot`을 보내고 선택 재계산 때 모든 client에 `renderer.assignment`의 `mode`와 `selected`를 보낸다. catalog에서 선택된 connection의 assignment에는 logical `renderer_id`도 포함된다. 공개 정책은 `metadata_only`이다.

Settings는 연결·인증된 singleton과 catalog의 각 logical renderer를 표시하며 설정에는 logical `selected_renderer_id`와 `mode`만 저장한다. Engram은 외부 path, command, executable, asset, worker 정보를 읽지 않는다. catalog가 없는 v2 renderer도 그대로 호환된다. catalog provider는 assignment의 logical ID에 해당하는 hidden surface를 준비한 뒤 exact `renderer.ready {renderer_id}`를 보내며, Engram은 일치하는 readiness 뒤에만 replace owner로 승격한다. 여러 observer와 하나의 selected replace owner를 허용한다. 의미 이벤트는 broadcast할 수 있지만 `overlay.show`, `overlay.hide`, `overlay.set_position`, `overlay.set_size`는 replace owner에게만 보낸다. catalog에서는 active item의 capabilities를 적용하며 collapse는 hide ACK 뒤 launcher를 보이고 timeout fallback에서도 ACK 전 pointer는 억제한다.

renderer가 보낼 수 있는 메시지는 다음뿐이다.

- `renderer.ready`: catalog provider의 pending logical item에 대한 exact `{renderer_id}`
- `overlay.geometry_changed`: integer `x/y`(-1,000,000..1,000,000), `width/height`(1..100,000)
- `overlay.visibility_changed`: boolean `visible`, replace owner only
- `pointer.action`: action만 또는 `right_click`/`drag_move`/`drag_end`에 필요한 integer `screen_x/screen_y`
- `overlay.heartbeat`: empty payload

추가·중첩 field, stale auth, duplicate ID, malformed/oversize JSON, 2초 등록 timeout, 16개 connection 제한(미등록 포함), client별 초당 120개 rate 제한을 위반하면 해당 client를 거부한다. inbound는 renderer ID와 role에 귀속되어 observer 입력이 replace 상태로 적용되지 않는다.

`conversation.input_active`의 `input` hint는 실제 keyboard/cursor 편집이 시작됐음을 뜻한다. 700ms inactivity, focus/hide/submit에는 `conversation.input_idle`의 `idle` hint가 오며 `conversation.input_submitted`는 제출 의미를 유지한다. 원문·thinking·tool input/output·path·token은 전송하지 않는다.

host 또는 renderer 시작 순서가 바뀌거나 연결이 끊기면 renderer는 bounded backoff로 discovery를 다시 읽어 새 instance에 등록해야 한다. 기존 command manifest는 실행하지 않고 진단하며 bundled renderer로 fallback한다.
