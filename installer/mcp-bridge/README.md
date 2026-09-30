# Engram MCP recovery bridge

The normal source and frozen installers register one per-client stdio bridge under
the existing `engram` server key. Claude and Codex native hook definitions and
approval records stay in their existing provider-owned files. Configuration files
receive unique backups before replacement; repeated installation is idempotent.

The bridge subscribes to the overlay's authenticated `/state/mcp/events` SSE stream.
It keeps client stdio open during backend replacement, internally initializes the
new MCP transport, and accepts subsequent calls when ready. Sent requests whose
outcome is unknown fail explicitly and are never automatically replayed. Calls
made while unavailable fail promptly. The bridge process itself must remain alive.

Existing sessions using direct HTTP need a one-time client MCP reload or a new
client session to adopt stdio. Subsequent backend restarts do not require it.

The provider-specific plugin templates are an optional distribution format, not
an additional installation on top of the stable server registration. Plugin server
namespaces differ by provider and existing native hook references target `engram`;
do not enable a second copy alongside the registered bridge. Build the console
artifact with `pyinstaller engram-mcp-bridge.spec`; the main overlay spec collects
that executable automatically. Plugin bundles must include the executable and
resolve their command/discovery paths before installation.

Optional local plugin bundles can be assembled with
`python installer/assemble_mcp_bridge_plugins.py --executable <console.exe> --output <final-local-bundle-directory>`.
This resolves executable and discovery paths without relying on provider-specific
environment interpolation. Reassemble if moving that local directory; the installer
uses direct registration to retain the stable native hook server key.

For non-interactive Claude Code acceptance runs, set
`CLAUDE_CODE_MCP_STARTUP_WAIT_MS=120000` and `MCP_TIMEOUT=120000` in the
client process environment. Claude can otherwise begin its first model turn
before a cold frozen bridge has finished starting. These test settings do not
change user configuration; see the official
[environment variable reference](https://code.claude.com/docs/en/env-vars).
Codex registration sets `startup_timeout_sec` to at least 120 seconds.

Run transport acceptance with two persistent clients, actual backend replacement,
and an interrupted mutation receipt counter:

```powershell
python scripts/dev/mcp_bridge_acceptance.py --repo <checkout> --bridge <source-launcher-or-console-exe> --output <existing-output-directory>/bridge-report.json
```

The GUI overlay is not needed for this protocol test. Always stop fixture children
in `finally`; preserve the user's existing overlay and MCP process family.
