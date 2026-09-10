# Real-provider lifecycle diagnosis — 2026-09-08

## Scope

Disposable real Claude Code 2.1.263 and Codex CLI 0.153.4 processes ran in
Orca-managed test terminals. Each successful pair used a new conversation and
its exact provider ID for resume. These are not Orca chat-tab SDK tests.
The original read-only provider session was never resumed or modified.
No user hook/config file was changed and no installed overlay was restarted.

The probe asks for a safe title, a read-only lookup and a small computation.
It explicitly prohibits manually calling state reporters: state changes must
come from the provider's automatic lifecycle integration. Reports exclude raw
prompts, memory results, tool arguments and credentials. Native providers retain
their disposable test conversations for resume.

## Confirmed results

| Path | Result | Evidence |
| --- | --- | --- |
| Claude existing configuration, exact test-session resume | Title and lookup calls succeeded; Stop occurred and process exited successfully. Correlated row stayed unknown. | `logs/real-provider-claude-resume.json` |
| Claude temporary native lifecycle hooks, new conversation | Correlated row transitioned unknown at 1.33s, working at 4.31s, ready at 17.55s. Successful exit. | `logs/real-provider-claude-hooks.json` |
| Claude same temporary hooks, resumed conversation | unknown at 1.89s, working at 4.92s, ready at 15.92s. Successful exit. | Same report, resume phase |
| Codex existing configuration | Title and lookup calls failed with tool-policy-related errors. | `logs/real-provider-codex-delivery.json`; exact owned provider event records |
| Codex two per-invocation tool approvals | Both calls succeeded; title response was accepted=true, reason=delivered. | `logs/real-provider-codex-approved.json` |
| Codex resumed conversation | Lookup succeeded, but title tool was not called again. | Same report, resume phase |

The Codex A/B override allows only `engram_report_session_title` and
`engram_search_memories`. It preserves the read-only sandbox and does not edit
global configuration. The actual test runtime used Orca's Codex configuration
and the local Engram endpoint at 127.0.0.1:17385. That configuration explicitly
approved five older Engram tools but not the two diagnostic tools.

The current Claude user settings contained command hooks (including Orca hooks)
but no Engram native MCP lifecycle handlers. Adding only temporary generated
Engram lifecycle hooks restored observed state transitions. This establishes
the missing lifecycle integration boundary for this CLI path, not all providers.

## Limitations and failed attempts

- The first Claude attempts used a diagnostic budget that was too small.
  `real-provider-claude-baseline-v2.json` ended with `error_max_budget_usd`.
  Its unknown samples are supporting evidence only; the later successful resume
  is the completed baseline comparison.
- Codex did not consistently use the probe's requested title marker. No matching
  marker is not evidence of failed transport. Codex state/renderer attribution
  remains UNVERIFIED even when the title response is delivered.
- Actual generated-title delivery is demonstrated for external CLI calls, but
  automatic bubble title generation on an ordinary user request remains a
  separate acceptance gate.
- No global lifecycle hooks or new MCP tool approvals were installed. User
  approval is pending. Existing open sessions may need reload/reconnect after
  approved installation; this was not exercised against user sessions.
- Actual Orca chat-tab lifecycle and visible renderer animation remain
  UNVERIFIED. The separate native fixture does not replace those checks.
- Selection recovery and bubble title persistence are committed in `2db005c`.
  They are complementary fixes, not a substitute for lifecycle delivery.

## Next verification

After approved configuration changes, exercise the exact affected Orca launch
path with a disposable conversation and its resume. Verify genuine work and
completion signals, the correctly attributed monitor row, and the selected
external renderer. Codex's lifecycle hookup still needs its own implementation
and trust review; do not relabel it as Claude or infer work from ordinary MCP
traffic.
