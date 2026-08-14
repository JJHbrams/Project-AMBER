import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core.graph.semantic import stm_promoter
from core.memory.daily_checkpoint import append_daily_checkpoint


class AutoMemoryCheckpointTest(unittest.TestCase):
    def test_candidate_requires_completed_idle_exchange_and_turn_gap(self):
        rows = [
            {"id": i, "role": "user" if i % 2 else "assistant", "content": str(i), "timestamp": "2026-08-14 08:00:00"}
            for i in range(1, 11)
        ]
        session = {"id": 7}
        conn = unittest.mock.MagicMock()
        conn.execute.side_effect = [
            unittest.mock.MagicMock(fetchone=lambda: session),
            unittest.mock.MagicMock(fetchone=lambda: {"ts": ""}),
            unittest.mock.MagicMock(fetchall=lambda: list(reversed(rows))),
        ]

        with patch("core.graph.semantic.stm_promoter.get_connection", return_value=conn), patch(
            "core.graph.semantic.stm_promoter._load_auto_checkpoint_state",
            return_value={},
        ), patch("core.graph.semantic.stm_promoter.datetime") as dt:
            dt.now.return_value = datetime(2026, 8, 14, 9, 0, 0)
            dt.fromisoformat.side_effect = datetime.fromisoformat
            candidate = stm_promoter._get_auto_checkpoint_candidate(
                "overlay",
                idle_seconds=1800,
                min_user_turns=5,
            )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["user_turns"], 5)
        self.assertEqual(candidate["last_message_id"], 10)

    def test_daily_checkpoint_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "engram"
            external = Path(tmp) / "daily_notes"
            fake_kg = unittest.mock.MagicMock()
            with patch(
                "core.memory.daily_checkpoint.get_db_root_dir",
                return_value=str(db_root),
            ), patch(
                "core.memory.daily_checkpoint.get_kg",
                return_value=fake_kg,
            ):
                args = {
                    "checkpoint_id": "overlay-1-10",
                    "now": datetime(2026, 8, 14, 9, 30).astimezone(),
                    "summary": "directive 작업 완료",
                    "open_intents": "installer 실행",
                    "project_key": "project-engram",
                    "project_node_id": "graph-memory-roadmap",
                    "external_daily_dir": str(external),
                }
                first = append_daily_checkpoint(**args)
                second = append_daily_checkpoint(**args)

            self.assertTrue(first["engram_written"])
            self.assertTrue(first["external_written"])
            self.assertFalse(second["engram_written"])
            self.assertFalse(second["external_written"])
            text = (db_root / "docs" / "daily" / "2026-08-14.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("engram-checkpoint:overlay-1-10"), 1)
            self.assertIn("[[graph-memory-roadmap]]", text)
            fake_kg.resolve_links.assert_called()


if __name__ == "__main__":
    unittest.main()
