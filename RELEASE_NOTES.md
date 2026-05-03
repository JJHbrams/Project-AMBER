# Release Notes

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
