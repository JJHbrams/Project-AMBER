# Opt-in Claude session monitor hooks

Status: code-only. Real MCP transport with synthetic safe events is tested;
actual Orca Claude hook execution, templating, permission recovery and animation
remain UNVERIFIED until an explicitly authorized provider test. No hook settings
are installed automatically and this work does not restore persistent identity
or manual titles across connections.

The dedicated `engram_report_claude_event` tool receives native event metadata on
the already-connected MCP session. It never accepts a provider resume/session ID,
prompt, response, transcript path or tool input. Private prompt/tool identifiers
are hashed for ordering/approval correlation and never enter registry snapshots
or logs. `agent_name: Claude` is a participant display claim, not authentication.

## Generate and inspect

```powershell
python scripts/dev/claude_monitor_hooks.py --settings C:/explicit/path/settings.json
```

Dry-run is the default and prints only Engram's proposed handlers, not existing
settings/commands/credentials. It does not launch Claude, install MCP, connect a
server, or alter files. Use the configured MCP server name with `--server` when
it is not `engram`.

Only after explicit approval, add `--apply` to update that exact settings file.
Existing Orca/other command handlers and unrelated settings are preserved. Only
the selected server's `mcp_tool` handlers targeting `engram_report_claude_event`
are replaced. Existing settings are backed up to
`settings.json.engram-monitor-backup`; a pre-existing backup blocks a changed
re-apply. Inspect/archive that backup explicitly rather than overwriting it.

## Runtime constraints

- Requires native `mcp_tool` support and `prompt_id` (introduced in 2.1.196).
  Version 2.1.196 alone is not sufficient; the intended installed test target is
  2.1.263. The configured MCP server must already be connected.
- `UserPromptSubmit` and actual tool callbacks indicate working. `Stop` indicates
  a stop **attempt**, not a guaranteed final completion: another hook can block
  it, and a following actual callback returns to working. `StopFailure` is blocked.
- Native PermissionRequest has no `tool_use_id`. It is matched only to exactly
  one in-flight PreToolUse with the same safe tool name. Ambiguous/missing
  correlation conservatively holds needs-input until a terminal event/new turn;
  another tool finishing cannot clear that latch. No tool input is used.
  Subagent events and the lifecycle reporter's own tool callbacks are ignored.
- Only null/empty `agent_id` is accepted as root. An unresolved `${agent_id}`
  placeholder fails closed; actual native missing-field substitution is not yet
  verified. The tool matcher excludes the reporter as an additional recursion guard.
- A missed first prompt can recover on a later actual tool/terminal callback on
  the new connection. Known old turns cannot change the current turn. Bounded
  fences stay until actual transport disconnect/owner expiry, not idle heartbeat.
- Hook working expires to unknown after 120 seconds without another hook event.
  Ordinary MCP presence never renews that private lease. A genuinely long, quiet
  operation can therefore show unknown; no generic MCP working inference is used.
- `SessionEnd` retires that connection; no cached working is replayed. This does
  not securely restore logical-session identity or user title overrides after
  reconnect. Those are a separate unresolved feature.
- A title request is returned only on a supported context-bearing hook when the
  corresponding live row has no title. No transcript/body is used to derive one.
  Native hook text contains only documented non-blocking hook JSON fields;
  delivery diagnostics are separate MCP structured content.

Reference: [Claude Code hooks](https://code.claude.com/docs/en/hooks).
