---
name: engram-hook-trust
description: Safely inspect, back up, and explicitly restore the eight native Engram Codex MCP hook approvals without changing unrelated hooks or approvals.
---

# Engram Hook Trust

Use this skill only for the native Codex `engram_report_codex_event` MCP hooks.
It covers exactly: `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PermissionRequest`, `Stop`, `Interrupt`, `SubagentStart`, and `SubagentStop`.
It never manages `SessionStart`, titles, command hooks, arbitrary servers, or
other approval state.

Run `scripts/engram-hook-trust.mjs --help` first. The script uses Codex
app-server APIs and does not parse or rewrite TOML.

- `check` is read-only and reports finite status/reason codes. With `--backup`,
  it also reports how many snapshot entries are currently recoverable.
- `backup` is read-only against Codex. It creates a new immutable backup of
  the trusted-and-enabled subset only; it never adds an untrusted event.
- `restore` requires `--confirm --backup <file>`. It restores only a missing
  state record whose live canonical key and generated native definition hash
  exactly match the backup. It skips already trusted/enabled state and rejects
  existing untrusted/disabled state, changed definitions, unknown status, or
  root-mismatched state. An absent record reported as `untrusted` is eligible
  only when its unchanged approval was captured in this root's backup.

Never use a backup to manufacture approval for a changed definition, another
Codex root, or any hook outside this eight-event allowlist. Do not run restore
unless the user explicitly requests that specific restore operation.
Show the check result first: deletion alone cannot distinguish Orca's cleanup
from intentional user revocation. Never watch/restore automatically or import
approvals from `config.toml.bak`. If an approval disappeared before a valid
backup existed, ask the user to review it in Codex `/hooks`, then make a new
backup. Do not run a provider conversation or event hook to perform these tasks.

Typical invocation requires the native Codex executable, not the `codex.cmd`
npm shim: `node scripts/engram-hook-trust.mjs check --root "$env:CODEX_HOME"
--codex-executable <path-to-codex.exe>`. Backups must live under
`$env:CODEX_HOME/.engram-hook-trust/`.

Resolve the installed native executable and the target root explicitly; do not
mix the ordinary Codex home and Orca home. Run from the skill directory, or use
the absolute script path. Use a new filename for every backup. For example,
after setting task-local `$trustScript`, `$trustRoot`, `$trustExe`, and
`$trustBackup` to those verified paths:

```powershell
node $trustScript backup --root $trustRoot --backup $trustBackup --codex-executable $trustExe
node $trustScript check --root $trustRoot --backup $trustBackup --codex-executable $trustExe
# Only after the user requests this specific recovery:
node $trustScript restore --root $trustRoot --backup $trustBackup --codex-executable $trustExe --confirm
```

Report partial backup counts honestly. A backup with two records cannot restore
the other six hooks. A failed post-write verification may mean a write occurred;
inspect with `check` and stop instead of retrying or rolling back approvals.
