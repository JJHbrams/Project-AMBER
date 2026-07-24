# Release Notes

## 2026-07-24 - v0.1.1: One-Shot Native Installer, Offline Model, and Stability Fixes

### Highlights

- **One-shot native Windows installer (`EngramOverlay_0.1.1_x64-setup.exe`)** — double-click,
  GUI wizard (DB path / CLI provider / Ollama model / identity / autostart), and done.
  **No conda required on the user machine** — the backend runs inside the frozen binary.
- **Offline embedding model bundled** — `paraphrase-multilingual-MiniLM-L12-v2` (Apache-2.0) is
  packaged into the distribution, removing the HuggingFace download dependency at install/runtime.
- **Explicit "new session" for bubble chat** — reset the resident Claude session without
  restarting the overlay.
- **Reliability fixes** — restored vault→KG auto-sync (kg_sync), and fixed a Windows
  kg-watcher startup crash.

### What Changed

- `engram-overlay.exe` is now a **multi-call binary**: `--role mcp-server` / `--role kg-watcher`
  run the backend self-referentially, so `mcp_server` / `kg_watcher` no longer need a separate
  conda Python (`overlay/main.py`, `engram_overlay_entry.py`, `mcp_server.py`,
  `scripts/kg/kg_watcher.py`, `engram-overlay.spec`).
- New installer pipeline: `installer/build-installer.ps1` (PyInstaller freeze → Inno Setup
  compile → root `setup.exe`), `installer/engram-overlay.iss` (GUI wizard), and
  `installer/configure.ps1` (install-time config · MCP · shortcuts, pure PowerShell, no conda).
- Offline model: `engram-overlay.spec` bundles `resource/embedding-model`; the loader prefers
  the bundled model when frozen (`core/graph/semantic/semantic_graph.py`).
- Bubble new-session: `POST /bubble/new` on the overlay STM server (`overlay/stm_server.py`,
  `overlay/main.py`) + `.github/skills/engram-new-session` agent skill.
- Fixes: `/kg_sync` route awaited an unawaited coroutine (JSON-serialize failure);
  `kg-watcher` used `os.kill(pid, 0)` (unsafe on Windows) → `OpenProcess`; installer now always
  relaunches the overlay after install, targets shortcuts at the `.exe` (Pin-to-taskbar),
  ignores `__pycache__` for rebuild detection, falls back to safe defaults under
  `-NonInteractive`, and is saved as UTF-8 BOM for PowerShell 5.1.

### Impact

- End users install and run entirely offline after download — no conda setup, no HuggingFace
  fetch, no scattered TUI prompts.
- Distribution is a single `setup.exe`; re-running it upgrades in place (config/DB preserved).
- Overlay auto-sync (wiki→KG) works again; the overlay no longer crashes on kg-watcher startup.

### Files

- engram_overlay_entry.py, overlay/main.py, overlay/stm_server.py, mcp_server.py
- scripts/kg/kg_watcher.py, core/graph/semantic/semantic_graph.py, engram-overlay.spec
- installer/build-installer.ps1, installer/configure.ps1, installer/engram-overlay.iss
- installer/common.ps1, installer/modules/{06_db,07_shims,09_overlay,10_shortcuts}.ps1
- .github/skills/engram-new-session/SKILL.md, CHANGELOG.md

## 2026-05-03 - MCP Reconnect Stabilization and Transport Unification

### Highlights

- Migrated MCP client registration defaults from legacy SSE endpoints to `streamable-http`/`/mcp` for consistent reconnect behavior.
- Added hybrid MCP server routing so HTTP-native clients and legacy SSE clients can coexist during rollout.
- Added overlay-side MCP health monitoring and bounded auto-recovery to reduce dead-listener states after restarts.

### What Changed

- `config/config.yaml` now includes MCP runtime defaults for transport and health-check/recovery controls.
- `installer/modules/05_config.ps1` now emits HTTP `/mcp` registrations for Copilot/Claude/Gemini/VS Code/project-local configs.
- `mcp_server.py` now supports Windows HTTP event-loop policy setup and a hybrid streamable-http app that keeps `/sse` compatibility routes.
- `overlay/main.py` now launches transport by config, validates `/health`, and can recover MCP plus dependent dashboard/kg_watcher processes.
- `installer/common.ps1` documentation/comments were aligned with HTTP-first MCP usage.

### Impact

- Lower risk of split-brain MCP sessions when overlay and IDE tooling reconnect at different times.
- Better operator recovery path after overlay restarts without requiring a full VS Code restart.

### Files

- config/config.yaml
- installer/common.ps1
- installer/modules/05_config.ps1
- mcp_server.py
- overlay/main.py

## 2026-05-03 - Directive Compliance Enforcement (Prompt-Side)

### Highlights

- Added configurable directive enforcement modes to improve instruction compliance without building a full external orchestration pipeline.
- Improved prompt composition so directive blocks are treated as top-priority rules instead of ctx reference data.

### What Changed

- `core/context/directives.py` now supports `triggered`, `hybrid`, and `always` enforcement modes.
- `core/context/directives.py` now supports `pin_top_n` priority pinning and `max_items` injection caps to balance compliance and token usage.
- `core/context/context_builder.py` now injects directive blocks outside ctx wrappers and explicitly marks directive precedence.
- `core/config/runtime_config.py` now includes default directive enforcement settings and user override template comments.

### Impact

- Better directive adherence in normal chat/CLI flows with bounded token overhead.
- Reduced risk that directives are interpreted as passive context data.

### Files

- core/context/directives.py
- core/context/context_builder.py
- core/config/runtime_config.py

## 2026-05-01 - Claude Code (Ollama) Provider and Build Stability

### Highlights

- Added explicit `claude-code(ollama)` provider mode across overlay runtime, Discord routing, and installer setup.
- Added a dedicated installer selection flow that binds Claude Code to a chosen Ollama model.
- Hardened PyInstaller resource collection to avoid lock-file driven incremental build failures.

### What Changed

- `overlay/config.py`, `overlay/settings_window.py`, `overlay/main.py`, `overlay/character.py`, and `overlay/chat_window.py` now support `claude-code-ollama` as a canonical provider value.
- `installer/common.ps1`, `installer/modules/02_interactive.ps1`, and `installer/modules/07_shims.ps1` now support `claude-code(ollama)` selection and dispatch.
- `discord_bot/bot.py` now treats `claude-code-ollama` as resume-capable and routes execution with the selected Ollama model.
- `engram-overlay.spec` now filters Office lock/temp artifacts in `resource/character` during `datas` collection.

### Impact

- Operators can choose Claude direct mode or Claude-through-Ollama mode without manual config editing.
- Runtime/provider behavior is consistent between settings UI, tray menu, Discord bot, and installer.
- Incremental packaging is more stable in environments where Office/Explorer temp files appear under character assets.

### Files

- overlay/config.py
- overlay/settings_window.py
- overlay/main.py
- overlay/character.py
- overlay/chat_window.py
- installer/common.ps1
- installer/modules/02_interactive.ps1
- installer/modules/07_shims.ps1
- discord_bot/bot.py
- engram-overlay.spec
- config/overlay.yaml

## 2026-05-01 - Discord Routing and Queue Operations

### Highlights

- Added production-ready Discord routing for multi-guild and multi-channel deployments.
- Added channel FIFO queueing with bounded parallel workers across channels.
- Expanded operator docs for Discord setup, queue policy, and provider routing precedence.

### What Changed

- `discord_bot/bot.py` now supports explicit session commands, DM handling, and per-route provider resolution.
- `config/overlay.yaml` and `overlay/config.py` now expose routing, allow/deny, override, and queue control options.
- README Discord section was expanded with minimum and recommended config templates and operational behavior notes.

### Impact

- Requests remain ordered per channel under load while still processing multiple channels concurrently.
- Operators can set provider behavior at channel and guild levels without changing global defaults.
- Discord operations are easier to configure and troubleshoot with clearer policy and runtime guidance.

### Files

- discord_bot/bot.py
- config/overlay.yaml
- overlay/config.py
- README.md

## 2026-04-30 - Tutorial Step 4 Continuity Hardening

### Highlights

- Final tutorial flow now requires explicit session close before continuity can be completed.
- Next-session recall verification was stabilized to avoid same-session false positives.

### What Changed

- Added stronger step 4 phase-1 guidance and warnings so users know that save text alone is not enough.
- Applied scope-aware session resolution for continuity checks and session close linkage.
- Restricted tutorial debug bypass behavior to the active step verification path.

### Impact

- Users can reliably finish the final tutorial step after reopening a new session.
- Reduced confusion around “session saved but cannot complete” scenarios.

### Files

- core/tutorial/progress.py
- mcp_server.py
- overlay/stm_server.py
- test/test_tutorial_runtime.py
- test/test_tutorial_session_continuity_state.py

## 2026-04-28 - KG Graph Viewport Responsiveness

### Highlights

- The KG Graph view now expands to use available window space much more effectively.
- Streamlit iframe height is resized directly to match graph viewport updates.
- Default fallback graph heights were increased to reduce cramped rendering on first load.

### What Changed

- Added viewport-based graph height calculation (approximately 82%, with min/max clamping).
- Added direct frame resizing for embedded dashboard rendering.
- Updated tooltip pin bounds calculation to use live container height.

### Impact

- Better graph readability and interaction on larger windows.
- Less manual resizing and less vertical clipping during normal use.

### Files

- scripts/engram_dashboard.py

### Verification

- Python compile check passed for updated dashboard scripts.
- Browser validation confirmed expanded visible graph region after reload.
