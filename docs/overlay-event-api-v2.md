# External Overlay Event API v2

## Ownership and transport

Engram hosts an authenticated JSONL API on an OS-assigned TCP port bound only to `127.0.0.1`. It never starts, stops, locates, or passes arguments to an external renderer. The renderer owns its process and reconnect loop. Each UTF-8 line is one JSON object and is limited to 65,536 bytes.

## Discovery and authentication

The current-user discovery file is `~/.engram/overlay-event-api-v2.json`:

```json
{"schema_version":2,"host":"127.0.0.1","port":49152,"instance_id":"example-instance-id","token":"example-only-not-a-live-token"}
```

`host` is always the IPv4 loopback address and `port` is ephemeral. Engram creates an empty temporary file, removes inherited Windows ACLs and grants the current account full control (or applies mode `0600` elsewhere), verifies that operation, then writes and fsyncs the credentials and atomically replaces the discovery file. Failure to protect it prevents server publication. A fresh high-entropy token and instance ID are generated for every host start. Never copy the token to argv, configuration, logs, errors, Wiki pages, or telemetry.

The renderer reads discovery itself, connects, and sends this as its first line within 2 seconds:

```json
{"schema_version":2,"type":"overlay.register","payload":{"token":"value-read-from-discovery","instance_id":"current-instance-id","renderer_id":"vendor.renderer","name":"Vendor Renderer","supported_modes":["observer","replace"],"capabilities":["overlay.presentation"]}}
```

All shown values are placeholders. Required payload fields are `token`, `instance_id`, `renderer_id`, `name`, and `supported_modes`; `capabilities` is optional. `renderer_id` is 1–64 ASCII letters, digits, `.`, `_`, or `-`; `name` is a non-empty control-character-free string of at most 128 characters. Modes are a non-empty, duplicate-free list containing only `observer` and/or `replace`. Capabilities, when present, are a duplicate-free list of at most 32 non-empty control-character-free strings, each at most 128 characters. A bad token, stale instance, duplicate ID, malformed identity/capability, invalid or duplicate mode, or registration timeout closes the socket. Known capabilities are `overlay.presentation` and `overlay.set_size`; unknown bounded string capabilities are retained for forward compatibility but do not activate host behavior.

### Optional catalog provider

A provider connection may add `catalog`, a bounded list of at most 32 logical renderers. Each item has exactly required `renderer_id`, `name`, and `supported_modes` fields plus optional `capabilities`; their syntax and bounds are the same as the top-level fields. Item IDs must be unique, must differ from the provider ID, and must not collide with any connected singleton, provider, or other catalog item. A collision rejects the whole registration.

```json
{"schema_version":2,"type":"overlay.register","payload":{"token":"value-read-from-discovery","instance_id":"current-instance-id","renderer_id":"vendor.catalog-provider","name":"Vendor Catalog","supported_modes":["observer","replace"],"catalog":[{"renderer_id":"vendor.rabbit","name":"Rabbit","supported_modes":["observer","replace"],"capabilities":["overlay.presentation"]}]}}
```

Settings expands each catalog item as a logical renderer and persists only that item's stable `selected_renderer_id` and `mode`. Engram does not read provider paths, commands, executables, assets, or worker details. Registrations without `catalog` retain the singleton v2 behavior unchanged.

## Host messages

Every host event uses this envelope:

```json
{"schema_version":2,"id":"evt_example","sequence":1,"timestamp":"2026-01-01T00:00:00+00:00","type":"generation.started","display_hint":"generating","payload":{}}
```

After registration Engram sends `engram.welcome` containing `selected_schema_version: 2`, `content_policy: metadata_only`, assigned `mode`, boolean `selected`, and `host_instance_id`, followed by `state.snapshot` and an initial `renderer.assignment`. The same assignment message is sent whenever selection is recomputed, so initial connection and later changes use one deterministic state path. For the selected item on a catalog connection, assignment also contains that logical `renderer_id`; the provider switches its active preset without changing the authenticated socket. Once that exact item has created its hidden presentation surface, the provider sends the bounded control `{"schema_version":2,"type":"renderer.ready","payload":{"renderer_id":"vendor.rabbit"}}`. Only a matching, still-selected catalog item is then promoted to replace owner; stale or malformed readiness is ignored and bundled fallback remains visible. Singleton renderers retain immediate activation and do not send `renderer.ready`.

`state.snapshot.display_hint` is the current resolved public visual state, not a fixed idle placeholder. Its payload contains only `generation_active` and the bounded `tool_category`. Engram keeps work, pointer hover, and active text input as separate layers: active input overrides hover, hover overrides work, and removing either layer reveals the still-active layer below it. Thus `conversation.input_idle` and `pointer.left` do not erase an in-progress search, memory lookup, or generation. Transient click/success/error messages may be animated by a renderer for its own declared dwell and do not replace the durable work layer.

Semantic events include generation, tool, provider, conversation, pointer, speech, and memory lifecycle hints. `conversation.input_active` (`display_hint: input`) means qualifying editing activity actually occurred; `conversation.input_idle` (`idle`) follows 700 ms inactivity or focus/hide/submit, while `conversation.input_submitted` remains submission. Payloads contain bounded metadata, never conversation text, thinking, tool input/output, paths, or tokens.

`overlay.show`, `overlay.hide`, `overlay.set_position {x,y}`, and `overlay.set_size {width,height}` are controls sent only to the selected replace owner. Presentation requires `overlay.presentation`; size requests require `overlay.set_size`. For a catalog provider, these checks use the active item's capabilities rather than the provider envelope. On collapse Engram sends `overlay.hide` first and does not reveal the launcher until `overlay.visibility_changed {visible:false}`; a bounded timeout restores the host launcher as an escape hatch while continuing to suppress late renderer pointer input until the authoritative acknowledgment. `overlay.show`/`hide` otherwise preserve the renderer presentation transition and visibility acknowledgment ordering. Control messages carry the current resolved `display_hint` and never reset visual work state. Other semantic events may be broadcast to all registered clients.

## Renderer messages

Only these exact schema-2 payloads are accepted; extra or nested fields are rejected:

- `renderer.ready`: `{renderer_id}`; catalog provider only, and only the exact pending logical item can be promoted.
- `overlay.geometry_changed`: `{x, y, width, height}`; all are integers, `x/y` are between -1,000,000 and 1,000,000, and dimensions are 1–100,000.
- `overlay.visibility_changed`: `{visible}` with a JSON boolean; replace owner only.
- `pointer.action`: `{action}` for `left_click`, `pointer_enter`, `pointer_leave`, `drag_begin`, `overlay_close`, or `menu_dismiss`; `{action, screen_x, screen_y}` for `right_click`, `drag_move`, or `drag_end`, with integer coordinates in the same screen range.
- `overlay.heartbeat`: an empty payload object.

Inbound messages retain their authenticated renderer ID and assigned role internally. Observer geometry can anchor that observer but cannot mutate replace geometry; observer visibility acknowledgements are rejected. The server permits at most 16 concurrent sockets including unregistered sockets and at most 120 inbound messages per second per client. Malformed JSON, oversize lines, invalid payloads, and rate violations close or ignore the offending client without revealing credentials.

## Selection, reconnect, and migration

Settings lists authenticated connected renderers and persists only `selected_renderer_id` and `mode`. Multiple observers may connect, but only the selected ready renderer can own replace. Selection changes notify every client deterministically and activate a ready replace owner immediately. When it disconnects, Engram restores its bundled renderer without ending provider sessions.

A renderer should reread discovery and retry with bounded exponential backoff when the file, host, or socket is unavailable. After a host restart it must discard stale port/token/instance data, reconnect, register, and consume the new welcome/snapshot.

Event API v1 command manifests are retired. A legacy `external_renderer.command` is diagnosed but never executed or bridged; Engram uses the bundled fallback until a v2 renderer connects.
