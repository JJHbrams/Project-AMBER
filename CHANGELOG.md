# Changelog

All notable changes to this project are documented in this file.

## [0.1.1] — 2026-07-24

### Added

- **통짜 native installer (Model B)** — `engram-overlay.exe` is now a multi-call binary
  (`--role mcp-server` / `--role kg-watcher`) so the backend runs self-referentially with
  **no conda dependency on the user machine**. Packaged as a single Inno Setup `setup.exe`
  (GUI wizard: DB path / CLI provider / Ollama model / identity / autostart) via
  `installer/build-installer.ps1`; install-time work is a lightweight `installer/configure.ps1`
  (config · MCP · shortcuts, pure PowerShell).
- **Offline embedding model bundle** — `paraphrase-multilingual-MiniLM-L12-v2` (Apache-2.0)
  is bundled into the frozen distribution, removing the HuggingFace download dependency at
  install/runtime.
- **Bubble-mode new session** — `POST /bubble/new` on the overlay STM server + an
  `engram-new-session` agent skill to explicitly reset the resident Claude session
  without restarting the overlay.

### Fixed

- **kg_sync HTTP route** returned an unawaited coroutine (`coroutine is not JSON
  serializable`), breaking kg_watcher's vault→KG auto-sync. Now awaits `kg_sync()` directly
  (thread offloading is inside the coroutine).
- **kg-watcher crash on Windows** — `_is_process_alive` used `os.kill(pid, 0)`, which on
  Windows attempts `TerminateProcess` and raises `SystemError` on dead PIDs. Replaced with
  `OpenProcess(SYNCHRONIZE)` liveness check. Backend `--role` crashes now log to stderr
  instead of a PyInstaller modal dialog.
- **Installer** — overlay is always stopped/relaunched on install (auto-update even when the
  build is skipped); Start Menu/Startup shortcuts target the `.exe` directly (enables
  "Pin to taskbar"); `__pycache__/*.pyc` no longer falsely triggers a rebuild; `Read-Host`
  prompts fall back to safe defaults under `-NonInteractive` (unattended install); installer
  scripts saved as UTF-8 BOM for Windows PowerShell 5.1 compatibility.

### Files

- engram_overlay_entry.py, overlay/main.py, overlay/stm_server.py, mcp_server.py
- scripts/kg/kg_watcher.py, core/graph/semantic/semantic_graph.py, engram-overlay.spec
- installer/build-installer.ps1, installer/configure.ps1, installer/engram-overlay.iss
- installer/common.ps1, installer/modules/{06_db,07_shims,09_overlay,10_shortcuts}.ps1
- .github/skills/engram-new-session/SKILL.md

## [2026-05-03]

### Added

- Added directive enforcement runtime settings (`directives.enforcement.mode`, `pin_top_n`, `max_items`) for configurable compliance behavior.

### Changed

- Directive selection now supports three enforcement modes: `triggered`, `hybrid`, and `always`.
- Hybrid mode now pins top-priority directives even when query triggers are absent, while capping injected directive count.
- System prompt composition now keeps directives outside ctx reference sections and marks directive blocks as highest-priority rules.
- MCP transport defaults were migrated to `streamable-http` (`/mcp`) across workspace, installer outputs, and user/global MCP config paths.
- Overlay startup now launches MCP transport from runtime config and applies listener health monitoring with bounded auto-recovery.
- MCP server `streamable-http` mode now serves a hybrid app that also exposes legacy SSE routes (`/sse`, `/messages`) during migration.

### Fixed

- Reduced multi-root transport mismatches by aligning generated client configs to HTTP endpoints, lowering chances of duplicate/stale server starts from tool UIs.
- Improved reconnect behavior after overlay/MCP restarts by adding readiness checks, recovery cooldown, and dependent-process restart orchestration.

### Files

- core/context/directives.py
- core/context/context_builder.py
- core/config/runtime_config.py
- config/config.yaml
- installer/common.ps1
- installer/modules/05_config.ps1
- mcp_server.py
- overlay/main.py

## [2026-05-01]

### Added

- Discord bot session control commands (`/session`, `/session list`, `/session use`, `/session new`, `/new`, `/newsession`, `/새세션`).
- Multi-route Discord configuration keys for guild/channel arrays, provider overrides, and scope_key override templates.
- Channel FIFO queue controls with bounded cross-channel concurrency, wait notices, and TTL-expiry notices.
- Added `claude-code-ollama` provider mode that binds Claude Code to the selected Ollama model.
- Added installer option `claude-code(ollama)` with model selection flow and dispatcher compatibility.

### Changed

- Provider selection now follows route precedence: channel override > guild override > current overlay default provider.
- Scope key resolution now follows route precedence: channel override > guild override > template > default channel scope.
- Discord runtime now keeps channel-scoped session continuity while supporting explicit new-session rollover on user command.
- Overlay provider menus now distinguish Claude direct mode and Claude-through-Ollama mode.

### Fixed

- Improved Discord runtime resilience around queued message handling and provider routing edge cases.
- Improved operational visibility with queue state and wait-position user notices.
- Hardened PyInstaller resource collection to skip Office lock/temp artifacts under `resource/character` that caused incremental build PermissionError.

### Files

- discord_bot/bot.py
- config/overlay.yaml
- overlay/config.py
- overlay/chat_window.py
- overlay/main.py
- overlay/character.py
- overlay/settings_window.py
- installer/common.ps1
- installer/modules/02_interactive.ps1
- installer/modules/07_shims.ps1
- engram-overlay.spec
- engram_overlay_entry.py
- docs/todo/todo.md

## [2026-04-30]

### Added

- Tutorial runtime warnings that explicitly require session close in step 4 phase 1 to complete continuity practice.
- Scope/session tracking fields for continuity review state (`saved_session_id`, `saved_scope_key`, `checked_session_id`).

### Changed

- Tutorial flow now enforces a strict two-phase continuity path: save-and-close first, then next-session recall verification.
- Tutorial debug bypass handling is constrained to the current step verification path only.

### Fixed

- Fixed false same-session detection in final tutorial verification by resolving active sessions with scope-aware lookup.
- Fixed STM close linkage so session close state is reflected before continuity completion checks.

### Files

- core/tutorial/progress.py
- mcp_server.py
- test/test_tutorial_runtime.py
- test/test_tutorial_session_continuity_state.py
- overlay/stm_server.py
- docs/todo/todo.md

## [2026-04-28]

### Added

- Viewport-responsive height behavior for the KG Graph dashboard panel.
- Direct iframe resizing logic for Streamlit-embedded graph content.

### Changed

- KG graph target height now follows parent viewport height (approximately 82%), clamped to a safe range.
- Graph container and tooltip pin positioning now use actual runtime container height.
- Fallback render heights were increased to provide a less cramped default layout.

### Fixed

- Resolved the issue where graph internals resized but visible area remained constrained by a fixed iframe height.

### Files

- scripts/engram_dashboard.py
- scripts/dev/engram_dashboard.py
