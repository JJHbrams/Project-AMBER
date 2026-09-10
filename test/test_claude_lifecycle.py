import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import uuid

from core.integrations.claude_lifecycle import (
    LifecycleTracker,
    TurnState,
    fold,
    validate_event,
)
from core.integrations.mcp_presence import PresenceReporter
from overlay.session_registry import SessionStateRegistry, CLAUDE_WORKING_LEASE_SECONDS
from overlay.state_api import validate_payload
from scripts.dev.claude_monitor_hooks import configure, generated_hooks, merge_settings


class LifecycleTests(unittest.TestCase):
    def test_real_event_mapping_old_turn_and_follow_on_work(self):
        state = TurnState()
        for event, expected in (
            ("UserPromptSubmit", "working"),
            ("PermissionRequest", "needs_input"),
            ("PostToolUse", "working"),
            ("Stop", "ready"),
            ("PreToolUse", "working"),
            ("StopFailure", "blocked"),
        ):
            self.assertEqual(fold(state, event, "turn-a", "tool-one"), (expected, None))
        self.assertEqual(fold(state, "UserPromptSubmit", "turn-b"), ("working", None))
        self.assertEqual(fold(state, "Stop", "turn-a"), (None, "stale_turn"))
        self.assertEqual(state.state, "working")
        self.assertEqual(
            fold(state, "Stop", "unknown-turn"), (None, "unestablished_turn")
        )

    def test_reconnect_can_first_observe_post_or_terminal(self):
        for event, expected in (
            ("PostToolUse", "working"),
            ("Stop", "ready"),
            ("PermissionRequest", "needs_input"),
            ("StopFailure", "blocked"),
        ):
            self.assertEqual(
                fold(TurnState(), event, "current", "tool-one"), (expected, None)
            )

    def test_parallel_permissions_settle_only_the_corresponding_tool(self):
        state = TurnState()
        fold(state, "PreToolUse", "turn", "one", "Read")
        fold(state, "PreToolUse", "turn", "two", "Edit")
        fold(state, "PermissionRequest", "turn", None, "Read")
        fold(state, "PermissionRequest", "turn", None, "Edit")
        self.assertEqual(
            fold(state, "PostToolUse", "turn", "one"), ("needs_input", None)
        )
        self.assertEqual(
            fold(state, "PreToolUse", "turn", "other"), ("needs_input", None)
        )
        self.assertEqual(
            fold(state, "PostToolUseFailure", "turn", "two"), ("working", None)
        )

    def test_idless_native_permissions_fail_conservatively_when_ambiguous(self):
        state = TurnState()
        fold(state, "PreToolUse", "turn", "one", "Read")
        fold(state, "PreToolUse", "turn", "two", "Read")
        self.assertEqual(
            fold(state, "PermissionRequest", "turn", None, "Read"),
            ("needs_input", None),
        )
        fold(state, "PostToolUse", "turn", "one", "Read")
        self.assertEqual(
            fold(state, "PostToolUse", "turn", "two", "Read"), ("working", None)
        )
        self.assertEqual(fold(state, "Stop", "turn"), ("ready", None))
        fresh = TurnState()
        self.assertEqual(
            fold(fresh, "PermissionRequest", "reconnect", None, "Read"),
            ("needs_input", None),
        )
        self.assertEqual(
            fold(fresh, "PostToolUse", "reconnect", "new", "Read"),
            ("working", None),
        )
        self.assertEqual(fold(fresh, "UserPromptSubmit", "next"), ("working", None))
        checked, error = validate_event(
            "PermissionRequest", str(uuid.uuid4()), tool_name="Read"
        )
        self.assertIsNone(error)
        self.assertIsNone(checked["tool"])
        self.assertNotIn(
            "tool_use_id",
            generated_hooks()["PermissionRequest"][0]["hooks"][0]["input"],
        )

    def test_ttl_and_ordinary_presence_do_not_erase_live_turn_fences(self):
        now = [0]
        tracker = LifecycleTracker(clock=lambda: now[0])
        reporter = PresenceReporter()
        reporter._claude_lifecycle = tracker
        reporter.submit = Mock()
        context = SimpleNamespace(
            request_context=SimpleNamespace(
                request=SimpleNamespace(
                    headers={}, scope={"engram.presence.transport_id": "connection"}
                )
            )
        )
        identifier = reporter.context_identity(context)[0]
        with tracker.transaction(identifier) as state:
            fold(state, "UserPromptSubmit", "A")
            fold(state, "UserPromptSubmit", "B")
        now[0] = 601
        reporter.report_context(context)
        with tracker.transaction(identifier) as state:
            self.assertEqual(fold(state, "Stop", "A"), (None, "stale_turn"))
            self.assertEqual(state.state, "working")

    def test_session_end_never_replays_in_same_connection(self):
        state = TurnState()
        self.assertEqual(fold(state, "SessionEnd", None), ("ended", None))
        self.assertEqual(
            fold(state, "UserPromptSubmit", "new"), (None, "session_ended")
        )

    def test_strict_safe_fields_and_nonroot_recursion_guards(self):
        turn = str(uuid.uuid4())
        for event, agent, tool in (
            ("Invalid", None, None),
            ("Stop", "agent", None),
            ("Stop", "${agent_id}", None),
            ("PreToolUse", None, "mcp__engram__engram_report_claude_event"),
            ("PreToolUse", None, "engram_report_claude_event"),
            ("PreToolUse", None, "mcp__my_engram__engram_report_claude_event"),
            ("PreToolUse", None, "C:/private"),
        ):
            self.assertIsNone(validate_event(event, turn, agent, tool)[0])
        for invalid in (None, "${prompt_id}", "private prompt", "x" * 129):
            self.assertIsNone(validate_event("Stop", invalid)[0])
        checked, error = validate_event("PreToolUse", turn, "", "Read")
        self.assertIsNone(error)
        self.assertNotIn(turn, json.dumps(checked))
        self.assertNotIn("Read", json.dumps(checked))

    def test_bounded_maps_keep_end_tombstones_until_disconnect(self):
        now = [1]
        tracker = LifecycleTracker(capacity=1, ttl=10, clock=lambda: now[0])
        with tracker.transaction("one") as state:
            state.ended = True
        now[0] += 11
        with tracker.transaction("two") as state:
            self.assertIsNone(state)
        self.assertTrue(tracker.ended("one"))
        tracker.forget("one")
        with tracker.transaction("two") as state:
            self.assertIsNotNone(state)
        state.retired = {str(i) for i in range(256)}
        state.turn = "old"
        self.assertEqual(
            fold(state, "UserPromptSubmit", "new"), (None, "turn_capacity")
        )

    def test_reporter_metadata_only_title_hint_and_own_connection(self):
        reporter = PresenceReporter()
        request = SimpleNamespace(
            headers={}, scope={"engram.presence.transport_id": "connection"}
        )
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))
        sent = []

        def send(payload, **kwargs):
            sent.append((payload, kwargs))
            if "title_status" in kwargs:
                kwargs["title_status"]["missing"] = True
            return True

        reporter._send = send
        turn = str(uuid.uuid4())
        result = reporter.report_claude_event(context, "PreToolUse", turn, "", "Read")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(sent[0][0]["agent_name"], "Claude")
        self.assertTrue(sent[0][1]["lifecycle"])
        self.assertNotIn(turn, repr(sent))
        self.assertNotIn("Read", repr(sent))
        repeated = reporter.report_claude_event(context, "PreToolUse", turn, "", "Read")
        self.assertNotIn("hookSpecificOutput", repeated)
        stopped = reporter.report_claude_event(context, "Stop", turn)
        self.assertNotIn("hookSpecificOutput", stopped)
        self.assertTrue(reporter.report_claude_event(context, "SessionEnd")["accepted"])
        self.assertEqual(
            reporter.report_claude_event(context, "Stop", turn)["reason"],
            "session_ended",
        )
        reporter._send = Mock(return_value=True)
        reporter.report_context(context)
        reporter._send.assert_not_called()

    def test_working_lease_not_extended_by_presence_or_metadata(self):
        now = [10.0]
        registry = SessionStateRegistry(clock=lambda: now[0])
        payload = {
            "provider": "mcp",
            "session_id": "one",
            "state": "working",
            "agent_name": "Claude",
        }
        row = registry.upsert_lifecycle(payload)
        registry.set_label(row["key"], "Manual title")
        registry.set_project_name("mcp", "one", "Project")
        now[0] += CLAUDE_WORKING_LEASE_SECONDS - 1
        registry.presence({"provider": "mcp", "session_id": "one"})
        self.assertEqual(registry.snapshot()[0]["state"], "working")
        now[0] += 2
        expired = registry.snapshot()[0]
        self.assertEqual(
            (
                expired["state"],
                expired["label"],
                expired["project_name"],
                expired["agent_name"],
            ),
            ("unknown", "Manual title", "Project", "Claude"),
        )
        registry.upsert_lifecycle({**payload, "state": "ready"})
        now[0] += CLAUDE_WORKING_LEASE_SECONDS + 1
        registry.presence({"provider": "mcp", "session_id": "one"})
        self.assertEqual(registry.snapshot()[0]["state"], "ready")
        registry.upsert(
            payload
        )  # Explicit ordinary state keeps its original semantics.
        now[0] += CLAUDE_WORKING_LEASE_SECONDS + 1
        self.assertEqual(registry.snapshot()[0]["state"], "working")
        registry.end_lifecycle("mcp", "one")
        self.assertIsNone(registry.presence({"provider": "mcp", "session_id": "one"}))
        self.assertIsNone(registry.upsert_lifecycle(payload))

    def test_agent_name_only_allowlisted_and_lease_not_public_input(self):
        for extra in (
            {"agent_name": "private/user"},
            {"agent_name": {}},
            {"lifecycle": True},
        ):
            self.assertIsNotNone(
                validate_payload(
                    {
                        "provider": "mcp",
                        "session_id": "one",
                        "state": "working",
                        **extra,
                    }
                )[1]
            )


class HookConfigurationTests(unittest.TestCase):
    def test_merge_preserves_orca_and_other_mcp_handlers(self):
        original = {
            "secret": "PRIVATE",
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {"type": "command", "command": "orca managed command"},
                            {
                                "type": "mcp_tool",
                                "server": "other",
                                "tool": "engram_report_claude_event",
                            },
                        ],
                    }
                ]
            },
        }
        before = copy.deepcopy(original)
        merged = merge_settings(original)
        self.assertEqual(original, before)
        self.assertEqual(merged["hooks"]["Stop"][0], original["hooks"]["Stop"][0])
        self.assertEqual(merge_settings(merged), merged)
        self.assertEqual(merged["secret"], "PRIVATE")
        for event, groups in generated_hooks().items():
            if event == "SessionStart":
                self.assertEqual(groups[0]["hooks"][0]["type"], "command")
                self.assertIn("engram-monitor-connection-title", groups[0]["hooks"][0]["command"])
                continue
            payload = groups[0]["hooks"][0]["input"]
            self.assertNotIn("session_id", payload)
            self.assertNotIn("prompt", payload)
            self.assertEqual(
                "tool_name" in payload,
                event
                in (
                    "PreToolUse",
                    "PostToolUse",
                    "PostToolUseFailure",
                    "PermissionRequest",
                ),
            )

    def test_dry_run_never_writes_and_apply_is_explicit_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            original = '{"secret":"PRIVATE"}'
            path.write_text(original, encoding="utf-8")
            result = configure(path)
            self.assertEqual(path.read_text(), original)
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertEqual(list(Path(directory).iterdir()), [path])
            configure(path, apply=True)
            self.assertEqual(
                path.with_name(path.name + ".engram-monitor-backup").read_text(),
                original,
            )
            self.assertFalse(configure(path, apply=True)["changed"])


if __name__ == "__main__":
    unittest.main()
