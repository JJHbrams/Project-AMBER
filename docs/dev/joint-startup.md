# Engram and external overlay startup

The host owns the Event API and saved renderer selection. The external package
owns its provider/worker processes. A connected provider supplies the settings
GUI catalog; installed files alone do not populate that catalog.

## Entry points

| Path | Runtime | External integration |
| --- | --- | --- |
| `dev-rebuild.ps1` | Current source checkout; does not build `dist` | Starts/reuses the installed catalog provider after verifying the new host family |
| `INSTALL.ps1` | Builds/updates the frozen developer installation | Installs or reuses the requested external runtime, applies startup choice, verifies the launched host and renderer |
| Setup EXE | Bundled frozen runtime | Uses the same startup/launch helpers through `configure.ps1` |

`-AutoStart preserve` preserves login registration. `on` enables joint startup;
`off` removes only registrations still matching Engram's ownership record.
The interactive source installer also offers host-only startup. Developer `on`
uses an existing installed host shortcut, never a disposable source worktree.

`-ExternalOverlay start|skip` controls the immediate external provider launch.
`-NoStart` suppresses final launches and login registration and cannot be combined
with explicit `-AutoStart on|off`. Full INSTALL still performs installation work;
it is not a dry run. Source INSTALL additionally supports
`-ExternalOverlayMode none|reuse|bolttagu-2d|later` for external runtime selection.

Examples from the intended checkout:

```powershell
.\dev-rebuild.ps1
.\dev-rebuild.ps1 -NoStart
.\INSTALL.ps1 -Reconfigure
.\INSTALL.ps1 -ExternalOverlayMode bolttagu-2d -AutoStart on
```

## Safety and readiness

Explicit canonical `user.config.yaml` service ports override legacy values;
existing `overlay.user.yaml` values remain the fallback. Entry handover, STM,
MCP child environment, dashboard client, and setup contracts use this resolver.
Invalid configuration fails before handover instead of silently using port 17384.

Handover verifies listener ownership, executable/command, creation time and the
same project before stopping a prior family. Unrelated listeners are preserved.
Process termination uses a pinned Windows handle rather than a later PID lookup.

Setup readiness checks current host/MCP parentage and the authenticated
`/state/renderers` snapshot. Catalog registration and selected worker readiness
are separate conditions. Discovery tokens never belong in logs or command lines.
Requested external integration failure is reported as incomplete setup.

User-owned external runtimes are reused only when compatible, never adopted by
adding an installer ownership marker. The joint startup record is separate from
runtime ownership. The external Run value launches `--provider` so all presets
can reconnect, regardless of host/provider login order.

## Verification boundary

`test/manual_joint_runtime.py` explicitly exercises isolated Windows source
restart, dev-rebuild, or source/frozen transitions with a kill-on-close Job.
It does not exercise the full INSTALL/Setup installation flow or real login.
Build validation uses a separate profile/DB and disables dashboard semantic HTTP.
Actual current-user installation tests still require preserving that user's
known-folder shortcuts, registry values and configuration; changing environment
variables alone does not isolate Windows installation side effects.

## Claude session monitoring

Compatible Claude Code installations receive eight native lifecycle hooks and a
separate SessionStart connection-title reminder through the common
`claude-monitor-hooks --provision --apply` role. `dev-rebuild` provisions them
only when starting; `-NoStart` does not change Claude settings. Source INSTALL
and EXE configure call the same role. Existing Orca and user hooks are preserved,
explicit `disableAllHooks` is respected, and changed settings are backed up.

MCP presence alone does not describe work. Native prompt/tool/stop events drive
working/attention/ready states; no arbitrary MCP call is treated as working.
Titles belong to the current connection. A resumed conversation must re-report
its safe title, even if an earlier connection was already titled. Neither the
hook installer nor the monitor reads Claude transcripts to construct titles.

MCP hooks require a connected server. A client retained across a server restart
can fail its first hook while holding a stale connection; client reconnection
must succeed before monitoring can recover. Do not claim that host readiness
proves every existing client has reconnected. Already-running Claude processes
also need to load the new hook settings.

`scripts/dev/real_provider_probe.py --natural-task` exercises actual new/resumed
CLI turns without instructing title or state calls in the user prompt.
`scripts/dev/claude_reconnect_probe.py --reconnect-between` exercises native MCP
reconnection while preserving the Claude process. Sanitized evidence and public
title hashes are retained, not raw model or hook bodies. These diagnostics are
explicit opt-in paid CLI runs, not automatically collected tests.
