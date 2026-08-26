# 커스텀 오버레이 Event API v1

## 설치와 선택

렌더러는 `%USERPROFILE%/.engram/overlays/<id>/manifest.yaml`에 설치한다. Settings > 오버레이에서 **기본 오버레이 사용** 또는 설치된 외부 오버레이를 선택하고, manifest가 허용한 `observer`/`replace` 모드만 고를 수 있다. 저장해도 실행 중인 자식은 바뀌지 않으므로 **오버레이를 재시작**해야 적용된다. renderer별 설정 화면이나 임의 command 실행은 제공하지 않는다.

```yaml
schema_version: 1
id: vendor-demo                 # 디렉터리명과 같아야 함
name: Vendor Demo
command: ["renderer.exe", "--engram-jsonl"]
supported_modes: [observer, replace] # 생략하면 observer만
```

`command`는 빈 값 없는 argv 문자열 배열이다. 첫 실행 파일이 상대 경로이면 manifest 디렉터리 밖으로 나갈 수 없으며 저장 시 절대 경로로 정규화된다. 버전·id·이름·명령·모드가 잘못됐거나 실행 파일이 없으면 목록에서 선택할 수 없고 Settings 상태 줄과 로그에서 진단한다.

## 커스텀 오버레이 → Engram — 보내야 하는 메시지

커스텀 오버레이 프로세스는 stdout JSONL로 Engram에 보낸다. 첫 줄은 반드시 `overlay.hello`이며 `payload.supported_schema_versions`에 `1`을 넣는다. `replace` 모드는 `overlay.geometry_changed`의 `x`, `y`, `width`, `height`를 창 생성·이동·크기 변경마다 보낸다. `pointer.action`에서 `left_click`, `pointer_enter`, `pointer_leave`는 좌표가 필요 없고, `right_click`, `drag_move`, `drag_end`는 `screen_x`, `screen_y`가 필요하다. `drag_begin`은 예약/no-op이며 `overlay.heartbeat`는 수신 후 무시되는 선택 메시지다.

```json
{"schema_version":1,"type":"overlay.hello","payload":{"supported_schema_versions":[1]}}
{"schema_version":1,"type":"overlay.geometry_changed","payload":{"x":120,"y":80,"width":320,"height":480}}
{"schema_version":1,"type":"pointer.action","payload":{"action":"left_click"}}
```

## Engram → 커스텀 오버레이 — 받는 메시지

Engram은 커스텀 오버레이 프로세스의 stdin에 JSONL로 `engram.welcome`, `state.snapshot`, replace 초기/보정 위치의 `overlay.set_position`, 그리고 의미 이벤트 envelope을 쓴다. replace에서 첫 `overlay.geometry_changed`는 renderer의 bootstrap 크기 보고로 취급한다. Engram에 저장된 x/y가 **authoritative**이며 renderer는 받은 초기 `overlay.set_position`을 최종 위치로 적용해야 한다. 첫 geometry의 width/height는 채택할 수 있지만 저장 위치를 덮어쓰지 않는다. 이후 사용자 이동 geometry는 정상적으로 위치를 갱신한다. `state.snapshot`은 현재 `display_hint: idle`, `payload: {generation_active: false, tool_category: null}`이다. envelope은 `schema_version`, `id`, `sequence`, `timestamp`, `type`, `display_hint`, `payload`를 가진다. hint는 `idle`, `hover`, `click`, `input`, `generating`, `search`, `thought`, `memory`, `success`, `provider_error`, `error`, `default`다.

`observer`는 기본값이며 번들 캐릭터를 유지한다. observer가 `geometry_changed`와 `left_click`을 보내면 Engram은 **같은 말풍선 세션**을 그 observer 창 위치로 앵커링한다. 이 좌표는 메모리에만 유지되며 번들 캐릭터 위치를 저장하거나 변경하지 않는다. geometry 없이 온 observer 클릭은 무시된다. 번들을 클릭하면 다시 번들 위치가 앵커가 된다. `replace`는 hello 성공 뒤 번들을 숨기고 geometry를 말풍선 앵커와 저장 위치로 사용한다. 자식 종료·잘못된 JSONL·handshake 실패에는 번들 창을 복구하고 열린 풍선도 번들 기준으로 다시 배치한다. API는 `metadata_only`라 대화 원문·thinking·도구 내용·파일 경로를 보내지 않는다. 진단이 필요하면 manifest 경로, schema/id 일치, 실행 파일 존재, stdout 첫 hello를 순서대로 확인한다.

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
