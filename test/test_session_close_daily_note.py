import unittest
from unittest.mock import MagicMock, patch

from core.memory import store


class SessionCloseDailyNoteTests(unittest.TestCase):
    def _connection_for(self, *, ended_at=None):
        conn = MagicMock()
        session_cursor = MagicMock()
        session_cursor.fetchone.return_value = {
            "scope_key": "project:daily-note-test",
            "ended_at": ended_at,
        }
        projects_cursor = MagicMock()
        projects_cursor.fetchall.return_value = [{"project_key": "daily-note-test"}]
        messages_cursor = MagicMock()
        messages_cursor.fetchall.return_value = []
        update_cursor = MagicMock()
        update_cursor.rowcount = 1
        conn.execute.side_effect = [session_cursor, projects_cursor, messages_cursor, update_cursor]
        conn.__enter__.return_value = conn
        return conn

    def test_close_records_daily_note_through_the_shared_close_coordinator(self):
        conn = self._connection_for()
        with patch.object(store, "get_connection", return_value=conn), patch.object(
            store, "resolve_kg_node_id", return_value="daily-note-test"
        ), patch.object(store, "append_session_close_daily_note") as append_note:
            store.close_session(42, "정상 종료", "다음 작업 확인", "진행 내용", "explicit")

        update_sql = conn.execute.call_args_list[3].args[0]
        self.assertIn("ended_at IS NULL", update_sql)
        append_note.assert_called_once()
        kwargs = append_note.call_args.kwargs
        self.assertEqual(kwargs["session_id"], 42)
        self.assertEqual(kwargs["project_key"], "daily-note-test")
        self.assertEqual(kwargs["project_node_id"], "daily-note-test")
        self.assertEqual(kwargs["open_intents"], "다음 작업 확인")
        self.assertEqual(kwargs["progress"], "진행 내용")
        self.assertFalse(kwargs["automatic"])

    def test_close_retry_reuses_the_same_session_marker(self):
        conn = self._connection_for(ended_at="2026-08-14 11:00:00")
        with patch.object(store, "get_connection", return_value=conn), patch.object(
            store, "resolve_kg_node_id", return_value=None
        ), patch.object(store, "append_session_close_daily_note") as append_note:
            store.close_session(42, "재시도", origin="explicit")

        self.assertEqual(append_note.call_args.kwargs["session_id"], 42)
        self.assertEqual(append_note.call_args.kwargs["now"].strftime("%Y-%m-%d"), "2026-08-14")


if __name__ == "__main__":
    unittest.main()
