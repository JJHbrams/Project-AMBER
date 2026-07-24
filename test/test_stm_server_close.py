import unittest
from unittest.mock import MagicMock, patch

from overlay.stm_server import _resolve_open_session_id


class STMServerCloseSessionTests(unittest.TestCase):
    @patch("core.storage.db.get_connection")
    def test_explicit_session_id_has_priority(self, mock_get_connection):
        self.assertEqual(_resolve_open_session_id("42", "overlay"), 42)
        mock_get_connection.assert_not_called()

    @patch("core.storage.db.get_connection")
    def test_scope_based_lookup_returns_latest_open_session_id(self, mock_get_connection):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = [77]
        mock_get_connection.return_value = conn

        self.assertEqual(_resolve_open_session_id(None, "overlay"), 77)

    @patch("core.storage.db.get_connection")
    def test_returns_none_when_no_open_session(self, mock_get_connection):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        mock_get_connection.return_value = conn

        self.assertIsNone(_resolve_open_session_id(None, "overlay"))

    def test_invalid_session_id_returns_none(self):
        self.assertIsNone(_resolve_open_session_id("invalid", "overlay"))


if __name__ == "__main__":
    unittest.main()
