---
name: engram-connect
description: Recover a local Codex-to-Engram MCP connection with bounded loopback-only diagnostics.
---

# Engram Connect

Use this only when Codex's Engram MCP tools are absent or connection recovery is requested.
First inspect the configured Codex MCP entry and report what is missing. Never alter
provider configuration or hook trust as part of a diagnosis.

For a direct, read-only MCP recovery probe, explicitly identify the Codex root and
run `node scripts/engram-connect.mjs probe --root <Codex-root>`. It reads only that
root's `config.toml`, accepts an unauthenticated `http://127.0.0.1` or
`http://localhost` Engram URL, initializes Streamable HTTP MCP, and lists tools.
It never changes config, hooks, trust, headers, or provider state.

The only data calls are allowlisted and bounded:

- `node scripts/engram-connect.mjs search --root <Codex-root> --query <keywords>`
- `node scripts/engram-connect.mjs read-note --root <Codex-root> --id <note-id>`

The helper rejects remote URLs, HTTPS, authorization/header configuration, unknown
tools, oversized config/output, and malformed MCP responses. It sends no conversation
content, credentials, raw memory, or arbitrary tool payloads.

If the service is reachable but tools remain unavailable, try the explicit live
reconnect below first when an existing managed daemon and its target thread are
available. Start a new Codex session only when that bounded reconnect is unsupported
or there is no such existing daemon/thread. Never add an automatic retry loop,
watchdog, or repeated reload polling.
Use `engram-hook-trust` separately for explicit `/hooks` review or a user-requested
restore; this skill never approves hooks.

## Explicit live Codex reconnect

When the running Codex session itself is missing Engram tools, use the bounded
reconnect command below. It attaches only to the already-running daemon through its
control socket; it never starts an app-server, thread, or conversation, and never
writes Codex configuration, hook trust, or approvals.

```powershell
node scripts/engram-connect.mjs reconnect --root <CODEX_HOME> --thread-id <CODEX_THREAD_ID> --codex-executable <path-to-codex.exe>
```

`--thread-id` may be omitted only when `--root` equals the current process's
`CODEX_HOME` and `CODEX_THREAD_ID` is set. The command first proves the daemon's
`initialize.codexHome` equals the requested root, then verifies the exact requested
thread is already loaded. It calls only `initialize` (with `experimentalApi`),
`initialized` (a notification), `thread/loaded/list`, `config/mcpServer/reload {}`, and
`mcpServerStatus/list` with `detail: toolsAndAuthOnly`.

The reload request is app-server-wide: it can refresh other already-loaded threads.
The helper never directly mutates those threads and only verifies the requested
thread. Its JSON output reports `requestAccepted` separately from
`engramToolsRestored`; a missing tool list is a failing exit status even if reload
was accepted. The short-lived proxy process is the only process it cleans up.
