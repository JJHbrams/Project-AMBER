import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from core.graph.semantic import stm_promoter
from core.memory.daily_checkpoint import append_daily_checkpoint, append_session_close_daily_note
from core.memory import daily_checkpoint


class AutoMemoryCheckpointTest(unittest.TestCase):
    def test_automatic_journal_parses_fenced_json(self):
        transcript = [{"role": "user", "content": "외부 일지를 사람이 읽기 좋게 정리해줘."}, {"role": "assistant", "content": "프로젝트별 저널 렌더러와 보존 테스트를 구현했다."}]
        with patch("core.memory.daily_checkpoint._call_journal_claude", return_value='```json\n{"title":"일지 개선","background":"","work":"렌더러 구현","result":"테스트 통과","next":""}\n```'):
            journal = daily_checkpoint._automatic_journal_from_transcript(transcript)
        self.assertEqual(journal["title"], "일지 개선")

    def test_meaningful_automatic_llm_failure_keeps_managed_ledger_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, external = Path(tmp) / "engram", Path(tmp) / "external"
            external.mkdir()
            with patch("core.memory.daily_checkpoint.get_db_root_dir", return_value=str(root)), patch(
                "core.memory.daily_checkpoint.get_kg", return_value=unittest.mock.MagicMock()
            ), patch("core.memory.daily_checkpoint.get_cfg_value", return_value=str(external)), patch(
                "core.memory.daily_checkpoint._call_journal_claude", return_value="not json"
            ):
                result = append_session_close_daily_note(session_id=88, now=datetime(2026, 8, 14, 9, 0).astimezone(), summary="", transcript=[{"role":"user","content":"저널 기능을 검토하고 문제를 정리해줘."}, {"role":"assistant","content":"문제를 확인하고 개선 방향을 제안했다."}], automatic=True)
            self.assertTrue(result["engram_written"])
            self.assertFalse(result["external_written"])
            self.assertFalse(list(external.glob("*.md")))

    def test_automatic_journal_rejects_auto_checkpoint_plumbing_but_keeps_managed_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, external = Path(tmp) / "engram", Path(tmp) / "external"
            external.mkdir()
            payload = '{"title":"auto-checkpoint 기록","background":"","work":"작업","result":"완료","next":""}'
            with patch("core.memory.daily_checkpoint.get_db_root_dir", return_value=str(root)), patch(
                "core.memory.daily_checkpoint.get_kg", return_value=unittest.mock.MagicMock()
            ), patch("core.memory.daily_checkpoint.get_cfg_value", return_value=str(external)), patch(
                "core.memory.daily_checkpoint._call_journal_claude", return_value=payload
            ):
                result = append_session_close_daily_note(session_id=89, now=datetime(2026, 8, 14, 9, 0).astimezone(), summary="", transcript=[{"role":"user","content":"저널 기능을 검토하고 문제를 정리해줘."}, {"role":"assistant","content":"문제를 확인하고 개선 방향을 제안했다."}], automatic=True)
            self.assertTrue(result["engram_written"])
            self.assertFalse(result["external_written"])
            self.assertFalse(list(external.glob("*.md")))
    def test_candidate_requires_completed_idle_exchange_and_turn_gap(self):
        rows = [
            {"id": i, "role": "user" if i % 2 else "assistant", "content": str(i), "timestamp": "2026-08-14 08:00:00"}
            for i in range(1, 11)
        ]
        session = {"id": 7}
        conn = unittest.mock.MagicMock()
        conn.execute.side_effect = [
            unittest.mock.MagicMock(fetchone=lambda: session),
            unittest.mock.MagicMock(fetchone=lambda: None),
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
            external.mkdir()
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

    def test_daily_checkpoint_skips_external_note_when_not_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "engram"
            fake_kg = unittest.mock.MagicMock()
            with patch(
                "core.memory.daily_checkpoint.get_db_root_dir",
                return_value=str(db_root),
            ), patch(
                "core.memory.daily_checkpoint.get_kg",
                return_value=fake_kg,
            ):
                result = append_daily_checkpoint(
                    checkpoint_id="overlay-2-20",
                    now=datetime(2026, 8, 14, 10, 0).astimezone(),
                    summary="외부 볼트 없는 사용자",
                    open_intents="",
                    project_key="general",
                    project_node_id=None,
                    external_daily_dir="",
                )

            self.assertTrue(result["engram_written"])
            self.assertFalse(result["external_written"])
            self.assertEqual(result["external_path"], "")

    def test_daily_checkpoint_does_not_create_missing_external_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "engram"
            external = Path(tmp) / "missing" / "daily_notes"
            with patch("core.memory.daily_checkpoint.get_db_root_dir", return_value=str(db_root)), patch(
                "core.memory.daily_checkpoint.get_kg", return_value=unittest.mock.MagicMock()
            ):
                result = append_daily_checkpoint(
                    checkpoint_id="overlay-3-30",
                    now=datetime(2026, 8, 14, 10, 0).astimezone(),
                    summary="외부 폴더 없음",
                    open_intents="",
                    project_key="general",
                    project_node_id=None,
                    external_daily_dir=str(external),
                )

            self.assertTrue(result["engram_written"])
            self.assertFalse(result["external_written"])
            self.assertFalse(external.exists())

    def test_session_close_daily_note_is_idempotent_and_managed_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_root = Path(tmp) / "engram"
            external = Path(tmp) / "daily_notes"
            external.mkdir()
            fake_kg = unittest.mock.MagicMock()
            with patch("core.memory.daily_checkpoint.get_db_root_dir", return_value=str(db_root)), patch(
                "core.memory.daily_checkpoint.get_kg", return_value=fake_kg
            ), patch("core.memory.daily_checkpoint.get_cfg_value", return_value=str(external)):
                args = {
                    "session_id": 42,
                    "now": datetime(2026, 8, 14, 11, 0).astimezone(),
                    "summary": "세션 마무리",
                    "open_intents": "다음 배포",
                    "scope_key": "overlay",
                }
                first = append_session_close_daily_note(**args)
                second = append_session_close_daily_note(**args)

            self.assertTrue(first["engram_written"])
            self.assertFalse(first["external_written"])
            self.assertFalse(second["engram_written"])
            self.assertFalse(second["external_written"])
            self.assertEqual(
                (db_root / "docs" / "daily" / "2026-08-14.md").read_text(encoding="utf-8").count(
                    "engram-checkpoint:session-close-42"
                ),
                1,
            )
            self.assertIn("- 다음 작업: 다음 배포", (db_root / "docs" / "daily" / "2026-08-14.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
