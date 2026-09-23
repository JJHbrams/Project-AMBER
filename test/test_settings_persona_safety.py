import functools
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import core.identity.service as identity_service
from core.storage.db import get_connection, initialize_db
from overlay.settings_window import _PERSONA_NUMERIC_FIELDS, _SettingsWindow


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Button:
    def __init__(self):
        self.states = []
        self.options = {}

    def state(self, value):
        self.states.append(value)

    def configure(self, **kwargs):
        self.options.update(kwargs)


class _Text:
    """tk.Text stand-in that, like Tk, ignores edits while disabled."""

    def __init__(self):
        self.value = ""
        self.widget_state = "normal"

    def configure(self, **kwargs):
        self.widget_state = kwargs.get("state", self.widget_state)

    def delete(self, *_args):
        if self.widget_state == "normal":
            self.value = ""

    def insert(self, _index, value):
        if self.widget_state == "normal":
            self.value += value


class PersonaOverwriteSafetyTests(unittest.TestCase):
    def _window(self, loaded=True):
        window = _SettingsWindow.__new__(_SettingsWindow)
        window.window = Mock()
        window._persona_load_ok = loaded
        window._persona_db_baselines = {"humor": 0.74} if loaded else {}
        window._persona_numeric_vars = {"humor": _Var(0.61)}
        window._persona_numeric_pin_vars = {"humor": _Var(True)}
        button = _Button()
        window._persona_numeric_overwrite_btns = {"humor": button}
        window._save_persona_user_file = Mock(return_value=0)
        return window, button

    def test_unloaded_persona_blocks_db_write_and_yaml_rewrite(self):
        window, _ = self._window(loaded=False)
        with patch("overlay.settings_window.set_persona_baseline") as set_db, patch(
            "overlay.settings_window.messagebox.showwarning"
        ) as warning:
            window._on_persona_overwrite("humor")
        set_db.assert_not_called()
        window._save_persona_user_file.assert_not_called()
        warning.assert_called_once()

    def test_cancel_does_not_change_db_or_yaml(self):
        window, button = self._window()
        with patch("overlay.settings_window.messagebox.askyesno", return_value=False) as ask, patch(
            "overlay.settings_window.set_persona_baseline"
        ) as set_db:
            window._on_persona_overwrite("humor")
        self.assertIn("현재 DB: 0.74", ask.call_args.args[1])
        self.assertIn("새 값: 0.61", ask.call_args.args[1])
        set_db.assert_not_called()
        window._save_persona_user_file.assert_not_called()
        self.assertEqual(button.options, {})

    def test_confirm_changes_only_selected_field_after_explicit_confirmation(self):
        window, button = self._window()
        with patch("overlay.settings_window.messagebox.askyesno", return_value=True), patch(
            "overlay.settings_window.set_persona_baseline"
        ) as set_db:
            window._on_persona_overwrite("humor")
        set_db.assert_called_once_with({"humor": 0.61})
        window._save_persona_user_file.assert_called_once()
        self.assertFalse(window._persona_numeric_pin_vars["humor"].get())
        self.assertEqual(window._persona_db_baselines["humor"], 0.61)
        self.assertEqual(button.options["text"], "완료✓")

    def _load_window(self):
        window = _SettingsWindow.__new__(_SettingsWindow)
        window._persona_banner_var = _Var("")
        window._persona_numeric_overwrite_btns = {f: _Button() for f in _PERSONA_NUMERIC_FIELDS}
        window._persona_numeric_vars = {f: _Var(0.5) for f in _PERSONA_NUMERIC_FIELDS}
        window._persona_numeric_pin_vars = {f: _Var(False) for f in _PERSONA_NUMERIC_FIELDS}
        window._persona_numeric_label_vars = {f: _Var("") for f in _PERSONA_NUMERIC_FIELDS}
        window._persona_lockable_widgets = [_Button(), _Button()]
        for name in ("voice", "traits", "quirks", "values", "fewshot"):
            setattr(window, f"_persona_{name}_txt", _Text())
        window._persona_fewshot_only_var = _Var(False)
        return window

    def _assert_all_locked(self, window, locked):
        expected = ["disabled"] if locked else ["!disabled"]
        for widget in (*window._persona_lockable_widgets, *window._persona_numeric_overwrite_btns.values()):
            self.assertEqual(widget.states[-1], expected)
        for widget in window._persona_text_widgets():
            self.assertEqual(widget.widget_state, "disabled" if locked else "normal")

    def test_unreadable_db_shows_user_yaml_but_locks_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_yaml = Path(tmp) / "persona.user.yaml"
            user_yaml.write_text("voice: 건조한 반말\ntraits: [직설]\nhumor: 0.9\n", encoding="utf-8")
            window = self._load_window()
            with patch("overlay.settings_window._USER_PERSONA_PATH", user_yaml), patch(
                "overlay.settings_window.is_persona_initialized", return_value=True
            ), patch("overlay.settings_window.get_persona_db_baseline", return_value={"humor": 0.74}):
                window._load_persona_values()
        self.assertFalse(window._persona_load_ok)
        self.assertEqual(window._persona_db_baselines, {})
        self.assertEqual(window._persona_voice_txt.value, "건조한 반말")
        self.assertEqual(window._persona_traits_txt.value, "직설")
        self.assertEqual(window._persona_numeric_vars["humor"].get(), 0.9)
        self.assertTrue(window._persona_numeric_pin_vars["humor"].get())
        self._assert_all_locked(window, True)
        self.assertIn("DB를 읽지 못했습니다", window._persona_banner_var.get())
        self.assertIn("잠겨", window._persona_banner_var.get())

    def test_invalid_user_yaml_fails_load_and_locks_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_yaml = Path(tmp) / "persona.user.yaml"
            user_yaml.write_text("- not a mapping\n", encoding="utf-8")
            window = self._load_window()
            with patch("overlay.settings_window._USER_PERSONA_PATH", user_yaml):
                window._load_persona_values()
        self.assertFalse(window._persona_load_ok)
        self._assert_all_locked(window, True)
        self.assertIn("로드 실패", window._persona_banner_var.get())

    def test_fresh_install_empty_db_persona_is_seeded_instead_of_failing(self):
        # New users reach this window from the tutorial before any model call has
        # seeded identity.persona; the empty row must not lock the persona tab.
        with tempfile.TemporaryDirectory() as tmp:
            initialize_db(tmp)
            window = self._load_window()
            with patch.object(
                identity_service, "get_connection", functools.partial(get_connection, tmp)
            ), patch("overlay.settings_window._USER_PERSONA_PATH", Path(tmp) / "missing.yaml"), patch.object(
                identity_service, "_USER_PERSONA_YAML_PATH", Path(tmp) / "missing.yaml"
            ):
                self.assertFalse(identity_service.is_persona_initialized())
                window._load_persona_values()
                self.assertTrue(identity_service.is_persona_initialized())
            self.assertTrue(window._persona_load_ok)
            self.assertEqual(set(window._persona_db_baselines), set(_PERSONA_NUMERIC_FIELDS))
            self._assert_all_locked(window, False)
            self.assertNotIn("로드 실패", window._persona_banner_var.get())

    def test_evolved_db_persona_is_not_reseeded(self):
        evolved = {"warmth": 0.54, "formality": 0.11, "humor": 0.73, "directness": 0.65}
        with tempfile.TemporaryDirectory() as tmp:
            initialize_db(tmp)
            conn = get_connection(tmp)
            with conn:
                conn.execute("UPDATE identity SET persona=? WHERE id=1", (json.dumps(evolved),))
            conn.close()
            window = self._load_window()
            with patch.object(
                identity_service, "get_connection", functools.partial(get_connection, tmp)
            ), patch("overlay.settings_window._USER_PERSONA_PATH", Path(tmp) / "missing.yaml"), patch.object(
                identity_service, "_USER_PERSONA_YAML_PATH", Path(tmp) / "missing.yaml"
            ):
                window._load_persona_values()
                self.assertEqual(identity_service.get_persona_db_baseline(), evolved)
        self.assertEqual(window._persona_db_baselines, evolved)
        self.assertEqual(window._persona_numeric_vars["humor"].get(), 0.73)

    def test_successful_reload_after_locked_load_unlocks_everything(self):
        window = self._load_window()
        with patch("overlay.settings_window.is_persona_initialized", return_value=True), patch(
            "overlay.settings_window.get_persona_db_baseline", return_value={}
        ):
            window._load_persona_values()
        self._assert_all_locked(window, True)
        good = {"warmth": 0.5, "formality": 0.5, "humor": 0.5, "directness": 0.5}
        with tempfile.TemporaryDirectory() as tmp:
            user_yaml = Path(tmp) / "persona.user.yaml"
            user_yaml.write_text("voice: 다시 열림\n", encoding="utf-8")
            with patch("overlay.settings_window._USER_PERSONA_PATH", user_yaml), patch(
                "overlay.settings_window.is_persona_initialized", return_value=True
            ), patch("overlay.settings_window.get_persona_db_baseline", return_value=good):
                window._load_persona_values()
        self.assertTrue(window._persona_load_ok)
        self._assert_all_locked(window, False)
        self.assertEqual(window._persona_voice_txt.value, "다시 열림")

    def test_skipped_persona_save_keeps_the_db_failure_banner(self):
        window = self._load_window()
        with patch("overlay.settings_window.is_persona_initialized", return_value=True), patch(
            "overlay.settings_window.get_persona_db_baseline", return_value={}
        ):
            window._load_persona_values()
        banner = window._persona_banner_var.get()
        window._ensure_user_persona_file = Mock()
        self.assertEqual(window._save_persona_user_file(), 0)
        self.assertEqual(window._persona_banner_var.get(), banner)
        window._ensure_user_persona_file.assert_not_called()

    def test_failed_load_save_skips_persona_yaml_rewrite(self):
        window, _ = self._window(loaded=False)
        window._persona_banner_var = _Var("")
        window._ensure_user_persona_file = Mock()
        self.assertEqual(window._save_persona_user_file(), 0)
        window._ensure_user_persona_file.assert_not_called()

    def test_save_keeps_persona_failure_visible_while_saving_other_settings(self):
        window, _ = self._window(loaded=False)
        window._do_save = Mock(return_value=0)
        window._on_saved = None
        window._policy_sync_warnings = []
        window._show_toast = Mock()
        window._update_persona_banner = Mock()
        with patch("overlay.settings_window.has_user_persona_override", return_value=False):
            window._save()
        window._update_persona_banner.assert_not_called()
        self.assertIn("일반 설정은 저장되었습니다", window._show_toast.call_args.args[0])

    def test_settings_close_callback_is_delivered_once_after_idempotent_close(self):
        window = _SettingsWindow.__new__(_SettingsWindow)
        window.window = Mock()
        window._closed = False
        window._pending_manual_provision = {}
        window._cancel_grid_eyedropper = Mock()
        window._remote_after_id = None
        window._on_closed = Mock()
        window._close()
        window._close()
        window.window.destroy.assert_called_once_with()
        window._on_closed.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
