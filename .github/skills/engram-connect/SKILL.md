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

If the service is reachable but tools remain unavailable, start a new Codex session.
Use `engram-hook-trust` separately for explicit `/hooks` review or a user-requested
restore; this skill never approves hooks.
