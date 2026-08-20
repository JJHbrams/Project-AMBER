import json
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

from overlay.stm_server import _STMHandler, _resolve_open_session_id


class STMServerCloseSessionTests(unittest.TestCase):
    @patch("overlay.stm_server.get_connection")
    def test_explicit_open_session_id_has_priority(self, mock_get_connection):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = [42]
        mock_get_connection.return_value = conn
        self.assertEqual(_resolve_open_session_id("42", "overlay"), 42)

    @patch("overlay.stm_server.get_connection")
    def test_explicit_ended_session_is_rejected(self, mock_get_connection):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        mock_get_connection.return_value = conn
        self.assertIsNone(_resolve_open_session_id("42", "overlay"))

    @patch("overlay.stm_server.get_connection")
    def test_scope_based_lookup_returns_latest_open_session_id(self, mock_get_connection):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = [[77]]
        mock_get_connection.return_value = conn

        self.assertEqual(_resolve_open_session_id(None, "overlay"), 77)

    @patch("overlay.stm_server.get_connection")
    def test_returns_none_when_no_open_session(self, mock_get_connection):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        mock_get_connection.return_value = conn

        self.assertIsNone(_resolve_open_session_id(None, "overlay"))

    def test_invalid_session_id_returns_none(self):
        self.assertIsNone(_resolve_open_session_id("invalid", "overlay"))

    @patch("overlay.stm_server.get_connection")
    def test_explicit_session_must_match_supplied_scope(self, mock_get_connection):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        mock_get_connection.return_value = conn
        self.assertIsNone(_resolve_open_session_id("42", "other-scope"))

    def test_http_close_forwards_summary_and_open_intents_to_shared_coordinator(self):
        server = HTTPServer(("127.0.0.1", 0), _STMHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            payload = json.dumps(
                {"session_id": 42, "summary": "HTTP 종료", "open_intents": "다음 단계"}
            ).encode("utf-8")
            request = Request(
                f"http://127.0.0.1:{server.server_port}/stm/session/close",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with patch("overlay.stm_server._resolve_open_session_id", return_value=42), patch(
                "overlay.stm_server.checkpoint_open_session", return_value={"status": "checkpointed"}
            ), patch("overlay.stm_server._explicit_session_status", return_value="open"), patch("overlay.stm_server._close_session") as close_session:
                with urlopen(request, timeout=2) as response:
                    body = json.loads(response.read().decode("utf-8"))

            self.assertEqual(body, {"status": "ok", "closed_session_id": 42})
            close_session.assert_called_once_with(42, "HTTP 종료", "다음 단계", "", "automatic", "", None, "")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_explicit_ended_session_is_conflict(self):
        server = HTTPServer(("127.0.0.1", 0), _STMHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/stm/session/close",
                data=json.dumps({"session_id": 280, "scope_key": "overlay"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with patch("overlay.stm_server._explicit_session_status", return_value="ended_session"):
                from urllib.error import HTTPError
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 409)
            self.assertEqual(json.loads(raised.exception.read().decode())["status"], "ended_session")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_http_busy_checkpoint_does_not_close(self):
        server = HTTPServer(("127.0.0.1", 0), _STMHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/stm/session/close",
                data=json.dumps({"session_id": 42, "scope_key": "overlay"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with patch("overlay.stm_server._explicit_session_status", return_value="open"), patch(
                "overlay.stm_server._resolve_open_session_id", return_value=42
            ), patch("overlay.stm_server.checkpoint_open_session", return_value={"status": "busy"}), patch(
                "overlay.stm_server._close_session"
            ) as close_session:
                from urllib.error import HTTPError
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 409)
            close_session.assert_not_called()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
