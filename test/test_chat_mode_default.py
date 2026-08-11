import unittest

from overlay.config import get_chat_mode, normalize_chat_mode


class ChatModeDefaultTests(unittest.TestCase):
    def test_chat_mode_defaults_to_bubble(self):
        self.assertEqual(normalize_chat_mode(None), "bubble")
        self.assertEqual(normalize_chat_mode("invalid"), "bubble")
        self.assertEqual(get_chat_mode({"overlay": {}}), "bubble")

    def test_chat_mode_preserves_explicit_tui(self):
        self.assertEqual(normalize_chat_mode("tui"), "tui")
        self.assertEqual(get_chat_mode({"overlay": {"chat_mode": "tui"}}), "tui")


if __name__ == "__main__":
    unittest.main()
