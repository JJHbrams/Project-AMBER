# 커스텀 오버레이 Event API v1

## 커스텀 오버레이 → Engram — 보내야 하는 메시지

커스텀 오버레이 프로세스는 stdout JSONL로 Engram에 보낸다. 첫 줄은 반드시 `overlay.hello`이며 `payload.supported_schema_versions`에 `1`을 넣는다. `replace` 모드는 `overlay.geometry_changed`의 `x`, `y`, `width`, `height`를 창 생성·이동·크기 변경마다 보낸다. `pointer.action`에서 `left_click`, `pointer_enter`, `pointer_leave`는 좌표가 필요 없고, `right_click`, `drag_move`, `drag_end`는 `screen_x`, `screen_y`가 필요하다. `drag_begin`은 예약/no-op이며 `overlay.heartbeat`는 수신 후 무시되는 선택 메시지다.

```json
{"schema_version":1,"type":"overlay.hello","payload":{"supported_schema_versions":[1]}}
{"schema_version":1,"type":"overlay.geometry_changed","payload":{"x":120,"y":80,"width":320,"height":480}}
{"schema_version":1,"type":"pointer.action","payload":{"action":"left_click"}}
```

## Engram → 커스텀 오버레이 — 받는 메시지

Engram은 커스텀 오버레이 프로세스의 stdin에 JSONL로 `engram.welcome`, `state.snapshot`, replace 초기/보정 위치의 `overlay.set_position`, 그리고 의미 이벤트 envelope을 쓴다. `state.snapshot`은 현재 `display_hint: idle`, `payload: {generation_active: false, tool_category: null}`이다. envelope은 `schema_version`, `id`, `sequence`, `timestamp`, `type`, `display_hint`, `payload`를 가진다. hint는 `idle`, `hover`, `click`, `input`, `generating`, `search`, `thought`, `memory`, `success`, `provider_error`, `error`, `default`다.

`observer`는 기본값이며 번들 캐릭터를 유지한다. `replace`는 hello 성공 뒤 번들을 숨기고 geometry를 말풍선 앵커로 사용한다. 자식 종료·잘못된 JSONL·handshake 실패에는 번들 창을 복구한다. API는 `metadata_only`라 대화 원문·thinking·도구 내용·파일 경로를 보내지 않는다.

```python
import json, sys
print(json.dumps({"schema_version": 1, "type": "overlay.hello", "payload": {"supported_schema_versions": [1]}}), flush=True)
for line in sys.stdin:
    try:
        event = json.loads(line)  # Engram -> 커스텀 오버레이
    except json.JSONDecodeError:
        print("invalid JSONL", file=sys.stderr)
        continue
    if event.get("type") == "overlay.set_position":
        print("move window:", event["payload"], file=sys.stderr)
    elif "display_hint" in event:
        print("play animation:", event["display_hint"], file=sys.stderr)
# stdout -> Engram: print(json.dumps({"schema_version":1,"type":"pointer.action","payload":{"action":"left_click"}}), flush=True)
```

```yaml
overlay:
  external_renderer:
    mode: "replace"
    command: ["C:/vendor/renderer.exe", "--engram-jsonl"]
```
