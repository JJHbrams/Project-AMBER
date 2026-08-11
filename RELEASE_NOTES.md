# Release Notes

## 2026-08-11 - v1.1.2: Bubble Mode by Default

> Patch release on the **1.1 line**. The installer is attached to the existing `Ver 1.1`
> release; use the newest asset.

### Changed

- **New installations now start in bubble mode.** `overlay.chat_mode` defaults to `bubble`
  when there is no user override.
- **Explicit TUI selections remain stable.** Choosing TUI in Settings writes `tui` to
  `overlay.user.yaml`, so the new default does not override that choice.

## 2026-08-07 - v1.1.1: Bubble Scrollbar, and a Bug That Wasn't Ours

> Patch release on the **1.1 line** — no separate GitHub Release. The installer is attached to
> the existing `Ver 1.1` release; grab the newest one from its **Assets**.

### Fixed

- **A response bubble taller than its maximum showed no scrollbar.** The content was clipped and
  there was no way to reach the rest. Both scroll mechanisms were switched off while the height
  was still being cut: the `needs_scroll` condition carried a `not used_html` guard, so the HTML
  path never built the canvas scrollbar, and `HtmlFrame` was constructed with
  `vertical_scrollbar=False`, disabling tkinterweb's own.

  The tk.Text fallback has no such guard — so this **only looked correct on machines where
  tkinterweb failed to load**. The working path was the backup path; both had the same bug.

  Now created with `vertical_scrollbar="auto"`, plus an explicit re-evaluation once `place()`
  fixes the final height. tkinterweb's `AutoScrollbar` only re-checks visibility when tkhtml
  fires `yscrollcommand`, and that callback does not fire when the height *shrinks* — `after_idle`
  also runs before tkhtml reflows, both confirmed by measurement, hence one short additional delay.

### Added

- **Thought-bubble detail option** (`bubble.thought_detail` = `full` | `brief`, exposed in
  Settings → 말풍선). Default `full`, preserving existing behavior.

  This started as "the thought bubble looks like an old version on this machine", and the
  investigation ended somewhere else: **it is not our bug.** Current Claude Code CLI (2.1.223)
  emits `thinking_delta` events with the `thinking` text blanked, sending only `estimated_tokens`
  — measured directly (4 `thinking_delta` events, 0 characters of text, `estimated_tokens: 100`).
  That number is what becomes "생각을 정리하는 중…". The overlay cannot reconstruct text it never
  receives, so `full` cannot bring it back.

  The split came from versions, not packaging per se: this machine runs the native standalone
  binary, which updates itself, while an npm install waits for the user. One side simply got the
  new behavior first — it was **ahead, not behind**.

  So the option is useful in the opposite direction: `brief` pins the bubble to a short status
  line even where the CLI does supply reasoning text, making machines look alike. Abbreviation
  happens at render time (the original is kept), so saving takes effect immediately without a
  restart.

## 2026-08-06 - v1.1.0: Remote Access, and the Embedding Call That Froze the Server

> Folds in v1.0.0 (authenticated remote listener, SSH reverse tunnel access), which was built
> but never separately released on this distribution repo.
>
> From this release on, **only major/minor bumps get their own release**. Patches (1.1.x) are
> published as additional installers attached to this same release — check **Assets** for the
> newest one.

### Highlights

- **engram is reachable from a remote machine.** Memory and the wiki still live in exactly one
  place — your local machine — and a remote session shares that one over an SSH reverse tunnel.
  The security model used to be entirely "bound to loopback = authenticated", which holds only
  because reaching `127.0.0.1:17385` already implies local execution rights. A tunnel breaks that
  equation: the machine on the far end has no local rights but does reach the port. **Reachable ≠
  authorized.** So the listener was split in two — port 17385 stays as it was (local, no auth),
  and 17386 requires a bearer token, enforces a per-principal tool deny-list, pins a scope, serves
  only MCP paths, and writes an audit line per call.

- **Embedding-backed calls no longer freeze the whole server.** The symptom was not "sometimes
  slow" — it was "these specific calls die every time", after hanging 120 seconds and dropping the
  transport. Two causes stacked. FastMCP invokes synchronous (`def`) tool functions directly on the
  event loop rather than handing them to a thread pool, and nearly every semantic tool —
  `kg_semantic_search`, `kg_add_note`, `kg_update_node`, and `engram_get_context`, which almost
  every session calls at startup — was declared `def`. **One slow call stalled the entire server**,
  including any other client attached at that moment. Underneath that, `SemanticGraph` shared a
  single `kuzu.Connection`; KuzuDB itself uses `AsyncConnection` with a connection *pool* when it
  needs concurrency, so sharing one connection was never a supported pattern.

- **The wiki is editable beyond three fixed slots.** `kg_update_node` only ever rewrote
  summary / Progress / open_intents. Anything else in the body — a stale URL in a header, an
  architecture description — had no path, and a remote session cannot touch vault files directly
  (WAL SQLite and embedded KuzuDB mean the *server* is shared, never the files). `kg_patch_section`
  replaces body content by heading.

### What Changed

- `SemanticGraph` moved to `kuzu.AsyncConnection` and the whole call path became async.
  `threading.RLock` → `asyncio.Lock` loses reentrancy, so every place that took the lock while
  already holding it was split into a `_locked` internal method.
- The real culprit was likely the embedding, not KuzuDB. `compute_embedding` / `_get_encoder`
  never touch KuzuDB — `SentenceTransformer` loading and `encode()` block the loop synchronously,
  and there is a quiet fallback path that downloads from the Hub when the local load fails. Both
  moved to `asyncio.to_thread`. Without this the `AsyncConnection` migration would have been
  correct on paper and still hung in practice.
- Read paths (`semantic_search` and friends) previously took no lock at all. Nothing had broken
  yet only because FastMCP happened to serialize every call onto one thread — the moment real
  parallelism arrived, mismatched cache indices could have returned quietly wrong search results.
  Reads are now covered by the same lock. Nine concurrency tests were added.
- `kg_add_note(subdir=...)` — a slash in `title` was stripped during slugification, so notes
  landed flat in `projects/` (lint caught it as a rule violation). The workaround,
  `note_type="projects/my-project"`, wrote that string into the DB `type`, which then failed the
  `NODE_TYPES` check on the next `kg_sync` and was demoted to `concept`. Location and type were
  sharing one parameter; they are now separate. Path traversal is guarded.
- Persona no longer stops at a project boundary. The directive read "always respond in the
  **engram persona** (Mnema)…", and in a project with its own CLAUDE.md the model read that as an
  identity belonging to the engram project and **explicitly declined to adopt it** — not a
  violation, a faithful reading of what was written. Naming a project inside an identity rule
  turns into a scope qualifier. Rewritten, and added to the install seed (`directives.json`), where
  it had been missing entirely.
- Removing a tunnel from the list no longer resurrects it — `stop()` left a `STATE_DOWN` entry in
  the dictionary, which the periodic refresh mistook for a live orphan and re-added.
- Character sprite can be mirrored horizontally, from Settings or the right-click menu (the menu
  toggle redraws immediately).

### Impact

- Working from a remote machine keeps one continuous memory instead of forking a second store.
  Share the server, never the files.
- The calls that used to hang — semantic search, note creation, section edits, and session
  bootstrap — return promptly, and one slow call can no longer take the server down with it.
- Existing installs upgrade in place via the new `setup.exe` (config/DB preserved).

### Notes

- Remote access is deliberately server-only. Do **not** share `D:/intel_engram` over SMB or sshfs —
  `engram.db` is in WAL mode and the semantic graph is embedded KuzuDB; both corrupt on a network
  filesystem. See `docs/remote-access.md`.
- kg_watcher still has a cross-process fallback that opens KuzuDB directly when the MCP server is
  unreachable. `AsyncConnection` pools connections within one process and does nothing for that
  case — a pre-existing risk, unchanged by this release.

## 2026-07-30 - v0.2.2: Initiative Feedback Loop, Reply Affordance, and a Rename

### Highlights

- **Initiative now closes the loop.** In v0.2.1 the character could speak on its own, but what
  happened next went nowhere — engagement was never observed, so no knowledge or preference could
  accumulate. Outcomes are now classified and recorded: `engaged` / `acknowledged_no_reply`
  (opened but never answered) / `ignored` (faded, or an unrelated new turn) / `late_engaged`.
  Each resolution writes to `activity_log`, auto-resolves the originating curiosity, and feeds
  the ignore-backoff.
- **Reply affordance** — an SNS-style REPLY arrow badge sits on the bubble's bottom corner
  (opposite the tail). Pressing it opens an **empty** input; nothing is ever sent automatically.
  The remark itself is prepended to the session prompt, so a two-word answer still carries context.
- **Answer whenever you like.** The 25-second dwell is only the window for *observing* an
  outcome, not for replying. Click the character at any time to see what it last said — if that
  was an autonomous remark, the reply path is still there. If it was an ordinary response, you
  just start a new conversation.
- **The continuum is now named Mnema** (formerly Arona) — Greek μνῆμα, "memory" and also "that
  which remains", the same idea as *engram* in another language.

### What Changed

- `Nudge` carries `topic` / `ref_id`, and the engine keeps the rendered remark in `_active_nudge`
  until its outcome is fixed — this is the single lock that guarantees one outcome per remark.
- Backoff accounting split from spacing: the gap and per-source cooldown are still paid upfront
  (so a second remark cannot fire while phrasing runs), but a remark that never reaches the screen
  is now refunded instead of being counted as ignored.
- `activity_log.detail` has a parse contract — everything before the first `" | "` is
  machine-readable (`source` / `outcome` / `latency`, no spaces in values), everything after is
  free text. `latency` is recorded because it cannot be reconstructed later: a remark dismissed in
  three seconds and one that sat through a 25-second fade are opposite signals.
- Diagnostic logging — the engine now reports *why* it stayed silent, once per state change
  (`enabled` / phrasing in flight / quiet hours / screen busy / not idle enough / gap not met).
- Fixes: every user message used to reset the ignore-backoff, so it never actually accumulated;
  fade completion was never reported to the engine, so "ignored" was never observed; STM promotion
  on session close raised a `TypeError` on a stale `session_id` argument and failed silently.

### Impact

- Autonomous remarks stop being one-off events. The ledger (`activity_log`) now accumulates
  per-source and per-hour engagement, which a later pass can feed into the reflection pipeline.
- Replying no longer puts words in your mouth — the input opens empty, and nothing leaves until
  you press Enter.
- Existing installs upgrade in place via the new `setup.exe` (config/DB preserved). The rename
  applies to new identity output only; past conversation records keep the old name, because they
  are records of what actually happened.

### Known Issue

- With `bubble.speech_fade: false`, a response bubble never leaves the screen, so the initiative
  gate (which requires an empty screen) stays closed and **autonomous remarks never appear**.
  This setting is not exposed in Settings, and editing `overlay.user.yaml` by hand requires an
  overlay restart. Keep fade on and tune `speech_dwell_ms` instead — clicking the character
  restores the last exchange anyway.

## 2026-07-28 - v0.2.1: Proactive Presence (Initiative), Interest/Memory Quality, and Bubble UX

> Folds in v0.2.0 (bubble max-height/scroll, grip resize, global engram bootstrap hook),
> which was not separately released on this distribution repo.

### Highlights

- **Proactive presence (initiative)** — in bubble mode the desktop character can now speak on
  its own when idle, surfacing unfinished work, open curiosities, git status, or a persona
  remark. **Off by default**; idle wait / minimum gap / quiet hours are configurable in
  Settings → Overlay. Phrasing is hybrid — a template fallback plus an isolated one-shot LLM
  pass in the character's persona voice; the resident chat session's STM/resume is never touched.
- **Recall last exchange on click** — clicking the character re-shows the last response (and your
  question) even after the bubble faded. Autonomous remarks (teal) are visually distinct from
  Q&A (response + user echo).
- **Interest / memory quality** — interests (themes) are now labeled by a Claude judgment at
  session close instead of raw noun extraction; curiosities dedupe and auto-expire so context
  injection stays fresh.

### What Changed

- New `overlay/bubble/initiative.py` — idle-tick engine with guards (idle wait, min gap, quiet
  hours, per-source cooldown, ignore-backoff) over four sources: unfinished work (`open_intents`)
  / curiosity / git / persona. Settings → Overlay "능동 발화" group + `config/overlay.yaml`
  `bubble.initiative`. Nudges render in the speech slot (teal) via `BubbleManager.show_nudge`
  and engage on click.
- Themes: message-text noun extraction removed; `core/graph/semantic/stm_promoter.py` now updates
  semantic interest labels via a Claude judgment at session close. MCP
  `engram_update_themes(text)` → `engram_update_themes(themes: list[str])`; `record_*_message`
  `update_themes` argument dropped.
- Curiosities: `add_curiosity(dedup=True)`, `expire_stale_curiosities` (14d) /
  `purge_processed_curiosities` (30d); context rule marks resolution via
  `engram_address_curiosity(id)`; dashboard graph shows pending only.
- v0.2.0 (folded): bubble `speech/thought_max_height_ratio` + scroll, grip height resize,
  global engram `SessionStart` hook (`core/integrations/engram_bootstrap.py`), PyInstaller
  Tcl/Tk bundling (`engram-overlay.spec`).

### Impact

- The overlay character becomes an ambient presence rather than a reactive chat box — the
  differentiator over a plain terminal chat.
- Cleaner interest/curiosity signals mean context injection reflects real topics instead of noise.
- Existing installs upgrade in place via the new `setup.exe` (config/DB preserved).

### Files

- overlay/bubble/{initiative (new), bubble_manager, bubble_window, shapes, session}.py,
  overlay/main.py, overlay/settings_window.py, config/overlay.yaml
- core/graph/semantic/stm_promoter.py, core/identity/{__init__, curiosity, service}.py,
  core/context/context_builder.py, core/dashboard/data_access.py, core/memory/bus.py, mcp_server.py
- core/integrations/engram_bootstrap.py (new), engram-overlay.spec, installer/engram-overlay.iss,
  CHANGELOG.md
- scripts/dev/cleanup_themes_curiosities.py (new), test/test_initiative.py (new),
  test/test_memory_bus.py

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
