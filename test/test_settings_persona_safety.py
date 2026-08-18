import unittest
from unittest.mock import Mock, patch

from overlay.settings_window import _SettingsWindow


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

    def test_load_failure_disables_every_overwrite_button(self):
        window, button = self._window()
        window._persona_numeric_overwrite_btns["warmth"] = _Button()
        window._persona_banner_var = _Var("")
        with patch("overlay.settings_window.get_persona_db_baseline", return_value={"humor": 0.74}):
            window._load_persona_values()
        self.assertFalse(window._persona_load_ok)
        self.assertEqual(window._persona_db_baselines, {})
        self.assertIn(["disabled"], button.states)
        self.assertIn("로드 실패", window._persona_banner_var.get())

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


if __name__ == "__main__":
    unittest.main()
