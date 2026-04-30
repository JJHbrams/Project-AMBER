# Release Notes

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
