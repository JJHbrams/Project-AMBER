# Release Notes

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
