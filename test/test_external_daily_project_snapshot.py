"""v2 external Daily Note project snapshots leave the managed ledger untouched."""

import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.memory import daily_checkpoint


class ExternalDailyProjectSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.external = self.root / "external"
        self.external.mkdir()
        self.kg = MagicMock()
        self.kg.get_node.return_value = {"title": "Project Display"}
        self.now = datetime(2026, 8, 20, 9, 0).astimezone()
        self.db_patch = patch("core.memory.daily_checkpoint.get_db_root_dir", return_value=str(self.root / "managed"))
        self.kg_patch = patch("core.memory.daily_checkpoint.get_kg", return_value=self.kg)
        self.db_patch.start(); self.kg_patch.start()

    def tearDown(self):
        self.kg_patch.stop(); self.db_patch.stop(); self.temp.cleanup()

    def _checkpoint(self, checkpoint_id: str, *, when=None, summary="first", intents="next", key="project-a", node="node-a"):
        return daily_checkpoint.append_daily_checkpoint(
            checkpoint_id=checkpoint_id, now=when or self.now, summary=summary, open_intents=intents,
            project_key=key, project_node_id=node, external_daily_dir=str(self.external),
        )

    @property
    def external_note(self):
        return self.external / "2026-08-20.md"

    def test_managed_ledger_keeps_two_markers_while_external_project_is_one_latest_snapshot(self):
        self._checkpoint("scope-1-1", summary="old", intents="old next")
        self._checkpoint("scope-1-2", when=self.now + timedelta(minutes=5), summary="latest", intents="latest next")
        managed = (self.root / "managed" / "docs" / "daily" / "2026-08-20.md").read_text(encoding="utf-8")
        external = self.external_note.read_text(encoding="utf-8")
        self.assertEqual(managed.count("engram-checkpoint:"), 2)
        self.assertEqual(external.count("engram-external-project-v2:node:node-a"), 1)
        self.assertIn("- summary: latest", external)
        self.assertIn("- open_intents: latest next", external)
        self.assertNotIn("engram-journal:", external)

    def test_second_project_and_general_get_separate_v2_blocks(self):
        self._checkpoint("a", node="node-a")
        self._checkpoint("b", key="project-b", node="node-b")
        self._checkpoint("g", key="", node=None)
        text = self.external_note.read_text(encoding="utf-8")
        self.assertIn("engram-external-project-v2:node:node-a", text)
        self.assertIn("engram-external-project-v2:node:node-b", text)
        self.assertIn("engram-external-project-v2:general", text)
        self.assertIn("## General", text)

    def test_legacy_headings_and_journal_markers_are_preserved(self):
        legacy = "# Legacy Project\n\n<!-- engram-journal:old -->\nold body\n"
        self.external_note.write_text(legacy, encoding="utf-8")
        self._checkpoint("new")
        text = self.external_note.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(legacy))
        self.assertIn("engram-journal:old", text)
        self.assertIn("engram-external-project-v2:node:node-a", text)

    def test_retry_is_idempotent(self):
        first = self._checkpoint("retry", summary="same")
        second = self._checkpoint("retry", when=self.now + timedelta(minutes=1), summary="same")
        self.assertTrue(first["external_written"])
        self.assertFalse(second["external_written"])
        self.assertEqual(self.external_note.read_text(encoding="utf-8").count("engram-external-project-v2:"), 1)

    def test_out_of_order_older_snapshot_cannot_overwrite_newer(self):
        self._checkpoint("new", when=self.now + timedelta(minutes=10), summary="newer")
        older = self._checkpoint("old", when=self.now, summary="older")
        self.assertFalse(older["external_written"])
        self.assertIn("- summary: newer", self.external_note.read_text(encoding="utf-8"))

    def test_concurrent_different_projects_do_not_lose_blocks(self):
        path = self.external_note
        barrier = threading.Barrier(2)
        results = []
        def write(identity):
            barrier.wait()
            results.append(daily_checkpoint._upsert_external_project_snapshot(
                path, identity=identity, title=identity, checkpoint_id=identity, now=self.now, summary=identity, open_intents="",
            ))
        threads = [threading.Thread(target=write, args=("node:first",)), threading.Thread(target=write, args=("node:second",))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        text = path.read_text(encoding="utf-8")
        self.assertEqual(results, [True, True])
        self.assertIn("node:first", text); self.assertIn("node:second", text)

    def test_replace_failure_leaves_external_file_unchanged(self):
        self.external_note.write_text("# preserved\n", encoding="utf-8")
        with patch("core.memory.daily_checkpoint.os.replace", side_effect=OSError("disk error")):
            result = daily_checkpoint._upsert_external_project_snapshot(
                self.external_note, identity="node:failure", title="Failure", checkpoint_id="failure", now=self.now, summary="new", open_intents="",
            )
        self.assertFalse(result)
        self.assertEqual(self.external_note.read_text(encoding="utf-8"), "# preserved\n")

    def test_malformed_v2_file_fails_closed_without_touching_legacy_text(self):
        malformed = "# preserved\n<!-- engram-external-project-v2:node:broken -->\n## missing snapshot end\n"
        self.external_note.write_text(malformed, encoding="utf-8")
        result = daily_checkpoint._upsert_external_project_snapshot(
            self.external_note, identity="node:new", title="New", checkpoint_id="new", now=self.now, summary="new", open_intents="",
        )
        self.assertFalse(result)
        self.assertEqual(self.external_note.read_text(encoding="utf-8"), malformed)

    def test_mixed_timezone_timestamp_fails_closed_without_replacing_v2_block(self):
        original = "\n".join((
            "<!-- engram-external-project-v2:node:node-a -->", "## Project", "<!-- engram-external-snapshot-v2:start -->",
            "- checkpoint_id: existing", "- updated: 2026-08-20T09:00:00",
            "- summary: preserved", "- open_intents: keep", "<!-- engram-external-snapshot-v2:end -->", "",
        ))
        self.external_note.write_text(original, encoding="utf-8")
        result = daily_checkpoint._upsert_external_project_snapshot(
            self.external_note, identity="node:node-a", title="Project", checkpoint_id="new",
            now=self.now, summary="must not replace", open_intents="",
        )
        self.assertFalse(result)
        self.assertEqual(self.external_note.read_text(encoding="utf-8"), original)

    def test_crlf_legacy_prefix_and_trailing_whitespace_survive_append_and_replacement(self):
        legacy = b"---\r\ntags:\r\n  - legacy\r\n---\r\n# Existing\r\nkeep  \t\r\n\r\n\r\n"
        self.external_note.write_bytes(legacy)
        first = daily_checkpoint._upsert_external_project_snapshot(
            self.external_note, identity="node:node-a", title="Project", checkpoint_id="first",
            now=self.now, summary="first", open_intents="",
        )
        after_first = self.external_note.read_bytes()
        second = daily_checkpoint._upsert_external_project_snapshot(
            self.external_note, identity="node:node-a", title="Project", checkpoint_id="second",
            now=self.now + timedelta(minutes=1), summary="second", open_intents="next",
        )
        after_second = self.external_note.read_bytes()
        self.assertTrue(first); self.assertTrue(second)
        self.assertTrue(after_first.startswith(legacy))
        self.assertTrue(after_second.startswith(legacy))
        self.assertIn(b"<!-- engram-external-project-v2:node:node-a -->\r\n", after_first)
        self.assertIn(b"- summary: second\r\n", after_second)
        self.assertNotIn(b"\n", after_second.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()
