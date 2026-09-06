# External Overlay Event API v1 (legacy)

Event API v1 used host-owned renderer commands and child-process stdin/stdout JSONL. It is retired: Engram no longer discovers command manifests and never executes a legacy `external_renderer.command`.

Implement new external renderers against [Event API v2](overlay-event-api-v2.md). Existing v1 configuration receives an actionable legacy diagnostic and safely falls back to the bundled renderer.
