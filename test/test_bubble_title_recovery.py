"""Real host/controller/config paths with controlled SDK messages; no provider call."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from claude_code_sdk.types import SystemMessage
from overlay import config
from overlay.bubble.session import BubbleSessionManager
from overlay.bubble.state import BubbleStateController
from overlay.main import OverlayApp
from overlay.session_registry import SessionStateRegistry


class BubbleTitleRecoveryTests(unittest.TestCase):
    def test_stale_ui_snapshot_cannot_follow_terminal_checkpoint(self):
        app, manager, state = self.host()
        self.confirm(manager, 'confirmed-race')
        key = state.session_id
        self.registry.set_producer_label('claude', key, 'Earlier safe title')
        stale = state.title_checkpoint()
        self.registry.set_producer_label('claude', key, 'Latest safe title')
        terminal = state.retire()
        app._checkpoint_bubble_title(manager, state, terminal)
        with patch('overlay.main.set_bubble_title_metadata') as enqueue:
            app._checkpoint_bubble_title(manager, state, stale)
            app._checkpoint_bubble_title(manager, state, terminal)
            enqueue.assert_not_called()
        self.assertTrue(config.flush_overlay_state())
        self.assertEqual(config.get_bubble_title_metadata('confirmed-race')['producer'],
                         'Latest safe title')

    def test_outdated_active_title_snapshot_is_rejected(self):
        app, manager, state = self.host()
        self.confirm(manager, 'confirmed-active')
        self.registry.set_producer_label('claude', state.session_id, 'Earlier safe title')
        stale = state.title_checkpoint()
        self.registry.set_producer_label('claude', state.session_id, 'Latest safe title')
        with patch('overlay.main.set_bubble_title_metadata') as enqueue:
            app._checkpoint_bubble_title(manager, state, stale)
            enqueue.assert_not_called()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="engram-bubble-title-")
        self.path_patch = patch.object(
            config, "_STATE_PATH", Path(self.directory.name) / "state.yaml"
        )
        self.path_patch.start()
        self.owner_patch = patch.object(config, "_BUBBLE_TITLE_OWNER", None)
        self.owner_patch.start()
        self.registry = SessionStateRegistry()

    def tearDown(self):
        self.assertTrue(config.flush_overlay_state())
        self.owner_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    def host(self, resume=None, saved=None):
        app = object.__new__(OverlayApp)
        state = BubbleStateController(
            self.registry, resume_session_id=resume, saved_title=saved
        )
        token = object()
        config.claim_bubble_title_owner(token)
        manager = BubbleSessionManager(
            cwd="C:/fixture/Project",
            resume_session_id=resume,
            state_controller=state,
            on_session_id=config.set_bubble_session_id,
            on_title_checkpoint=lambda checkpoint: app._checkpoint_bubble_title(
                manager, state, checkpoint
            ),
        )
        manager._title_owner = token
        app._bubble_session, app._bubble_state = manager, state
        state.prepare_attempt(resume)
        return app, manager, state

    def confirm(self, manager, provider_id):
        manager._handle_message(
            SystemMessage(subtype="init", data={"session_id": provider_id})
        )

    def test_production_host_creation_restore_and_old_session_guard(self):
        app = object.__new__(OverlayApp)
        app.chat, app.root = Mock(), Mock()
        app._bubble_session = None
        app._session_registry = self.registry
        app._stm_server = SimpleNamespace(listening=True)
        with patch("overlay.main.load_cfg", return_value={}), patch(
            "overlay.main.get_workdir", return_value=Path("C:/fixture")
        ), patch("overlay.main.StmBridge", return_value=Mock()), patch.object(
            BubbleSessionManager, "start"
        ):
            app._ensure_bubble_session()
            first = app._bubble_session
            self.confirm(first, "provider-host")
            self.registry.set_producer_label(
                "claude", app._bubble_state.session_id, "Host generated title"
            )
            app._checkpoint_bubble_title()
            app._ensure_bubble_session()  # Real stop/checkpoint then constructor wiring.
            second = app._bubble_session
            self.assertIsNot(first, second)
            first._persist_session_id("late-provider")
            self.assertEqual(config.get_bubble_session_id(), "provider-host")
            self.confirm(second, "provider-host")
            self.assertEqual(
                self.registry.snapshot()[0]["label"], "오버레이 세션"
            )
            def reset_with_late_callback(sid):
                self.assertIsNone(app._bubble_session)
                second._persist_session_id('late-during-reset')
                config.set_bubble_session_id(sid)
            app.root.after.side_effect = lambda delay, callback: callback()
            with patch('overlay.main.set_bubble_session_id', side_effect=reset_with_late_callback):
                app.new_bubble_session()
            self.assertIsNone(config.get_bubble_session_id())

    def test_retired_provider_callback_cannot_change_saved_resume_id(self):
        app, manager, state = self.host()
        self.confirm(manager, 'known-provider')
        state.retire()
        manager._persist_session_id('late-provider')
        self.assertEqual(config.get_bubble_session_id(), 'known-provider')

    def test_sdk_confirmed_roundtrip_preserves_producer_manual_and_clear(self):
        app, manager, state = self.host()
        self.confirm(manager, "provider-a")
        self.registry.set_producer_label(
            "claude", state.session_id, "Generated task title"
        )
        # Existing saved preferences remain intact, but are no longer editable/displayed.
        self.registry.restore_title_metadata(f'claude:{state.session_id}',
            {'producer': 'Generated task title', 'manual': '직접 정한 이름'})
        app._checkpoint_bubble_title()
        self.assertTrue(config.flush_overlay_state())
        saved = config.get_bubble_title_metadata("provider-a")
        self.assertEqual(
            saved, {"producer": "Generated task title", "manual": "직접 정한 이름"}
        )
        manager.stop()
        app2, manager2, state2 = self.host("provider-a", saved)
        self.assertFalse(state2.missing_title())
        self.confirm(manager2, "provider-a")
        self.assertEqual(self.registry.snapshot()[0]["label"], "오버레이 세션")
        self.registry.set_producer_label(
            "claude", state2.session_id, "Updated automatic title"
        )
        self.assertFalse(state2.set_label(None))
        app2._checkpoint_bubble_title()
        self.assertTrue(config.flush_overlay_state())
        self.assertEqual(
            config.get_bubble_title_metadata("provider-a"),
            {"producer": "Updated automatic title", "manual": "직접 정한 이름"},
        )
        manager2.stop()

    def test_same_id_retry_rotates_owner_and_rejects_old_preinit_title(self):
        app, manager, state = self.host(
            "provider-a", {"producer": "Saved task title", "manual": "Saved manual"}
        )
        self.confirm(manager, "provider-a")
        old = state.session_id
        state.prepare_attempt("provider-a")
        manager._attempt_generation = 2
        self.assertNotEqual(state.session_id, old)
        self.assertFalse(
            self.registry.set_producer_label("claude", old, "Late old title")
        )
        manager._handle_message(
            SystemMessage(subtype="init", data={"session_id": "wrong-id"}), generation=1
        )
        self.assertIsNone(state._confirmed_provider_id)
        self.registry.set_producer_label(
            "claude", state.session_id, "New current title"
        )
        self.confirm(manager, "provider-a")
        self.assertEqual(
            self.registry.title_metadata("claude:" + state.session_id),
            {"producer": "New current title", "manual": "Saved manual"},
        )
        options = manager._build_options()
        self.assertEqual(options.env["ENGRAM_BUBBLE_SESSION_ID"], state.session_id)
        self.assertEqual(
            options.mcp_servers["engram"]["headers"]["x-engram-bubble-owner"],
            state.session_id,
        )
        manager.stop()

    def test_fallback_different_id_and_reset_never_inherit(self):
        app, manager, state = self.host(
            "provider-a", {"producer": "Old task title", "manual": "Old manual"}
        )
        self.confirm(manager, "provider-a")
        state.prepare_attempt(None)
        self.confirm(manager, "provider-b")
        self.assertFalse(state.missing_title())
        self.assertEqual(self.registry.title_metadata(f'claude:{state.session_id}'),
                         {'producer': None, 'manual': None})
        self.assertIsNone(config.get_bubble_title_metadata("provider-a"))
        self.registry.set_producer_label(
            "claude", state.session_id, "Last current title"
        )
        config.set_bubble_session_id(
            None
        )  # Production reset ordering: clear before stop.
        manager.stop()
        self.assertTrue(config.flush_overlay_state())
        self.assertIsNone(config.get_bubble_session_id())
        self.assertIsNone(config.get_bubble_title_metadata("provider-b"))
        app2, manager2, state2 = self.host(
            "expected", {"producer": "Wrong saved title", "manual": None}
        )
        self.registry.set_producer_label(
            "claude", state2.session_id, "Unconfirmed old title"
        )
        self.confirm(manager2, "different")
        self.assertFalse(state2.missing_title())
        self.assertEqual(self.registry.title_metadata(f'claude:{state2.session_id}'),
                         {'producer': None, 'manual': None})
        manager2.stop()

    def test_stop_snapshot_survives_later_flush_but_old_lifetime_cannot_enqueue(self):
        app, manager, state = self.host()
        self.confirm(manager, "same-provider")
        self.registry.set_producer_label("claude", state.session_id, "Last before stop")
        manager.stop()  # Final snapshot and retirement are atomic in registry.
        app._bubble_session = None
        self.assertTrue(config.flush_overlay_state())
        self.assertEqual(
            config.get_bubble_title_metadata("same-provider")["producer"],
            "Last before stop",
        )
        old_owner = manager._title_owner
        app2, manager2, state2 = self.host(
            "same-provider", config.get_bubble_title_metadata("same-provider")
        )
        self.confirm(manager2, "same-provider")
        self.assertFalse(
            config.set_bubble_title_metadata(
                "same-provider",
                {"producer": "Stale old title", "manual": None},
                owner=old_owner,
            )
        )
        self.assertFalse(
            self.registry.set_producer_label(
                "claude", state.session_id, "Late retired title"
            )
        )
        manager2.stop()

    def test_unconfirmed_invalid_metadata_and_unchanged_tick_never_write(self):
        app, manager, state = self.host()
        self.registry.set_producer_label(
            "claude", state.session_id, "Unconfirmed task title"
        )
        app._checkpoint_bubble_title()
        self.assertFalse(config._STATE_PATH.exists())
        self.confirm(manager, "confirmed")
        app._checkpoint_bubble_title()
        self.assertTrue(config.flush_overlay_state())
        with patch("overlay.main.set_bubble_title_metadata") as save:
            for _ in range(20):
                app._checkpoint_bubble_title()
            save.assert_not_called()
        for metadata in (
            {"producer": "C:/private", "manual": None},
            {"producer": "Safe two", "manual": "bad\ntext"},
            {"producer": "Safe two", "manual": None, "transcript": "PRIVATE"},
        ):
            self.assertFalse(
                config.set_bubble_title_metadata(
                    "confirmed", metadata, owner=manager._title_owner
                )
            )
        manager.stop()

    def test_missing_title_instruction_is_per_turn_transport_only(self):
        app, manager, state = self.host("resumed")
        manager._stm_bridge = Mock()
        raw = {
            "type": "user",
            "message": {"role": "user", "content": "Visible original user text"},
        }

        async def send_once():
            manager._prompt_queue = asyncio.Queue()
            await manager._prompt_queue.put((1, raw))
            generator = manager._prompt_generator()
            result = await anext(generator)
            await generator.aclose()
            return result

        result = asyncio.run(send_once())
        self.assertEqual(result, raw)
        self.assertEqual(raw["message"]["content"], "Visible original user text")
        manager._stm_bridge.record_user.assert_called_once_with(
            "Visible original user text"
        )
        self.registry.set_producer_label("claude", state.session_id, "Known task title")
        # Simulate terminal completion between the fixture's two independent turns.
        manager._terminal_gate.set()
        self.assertEqual(asyncio.run(send_once()), raw)
        policy = manager._build_options().append_system_prompt
        self.assertIn("Do not generate or report a session title", policy)
        self.assertIn("do not repeat engram_get_context_once", policy)
        manager.stop()


if __name__ == "__main__":
    unittest.main()
