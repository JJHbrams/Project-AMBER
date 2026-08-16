import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json

from core.install.bootstrap import bootstrap_install
from core.context.directives import update_directive
from core.storage.db import get_connection


class InstallBootstrapTest(unittest.TestCase):
    def test_creates_wiki_starters_and_seeds_directives_without_overwrite(self):
        templates = Path(__file__).resolve().parents[1] / "installer" / "templates"
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            existing = db_dir / "docs" / "guides" / "wiki-guide.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("keep me", encoding="utf-8")

            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                first = bootstrap_install(db_dir, templates)
                second = bootstrap_install(db_dir, templates)

            self.assertEqual(existing.read_text(encoding="utf-8"), "keep me")
            self.assertTrue((db_dir / "docs" / "moc" / "000-HOME.md").is_file())
            wiki_management = (
                db_dir / "docs" / "protocols" / "wiki-management-guide.md"
            ).read_text(encoding="utf-8")
            self.assertIn("현재 프로젝트 context나 최근 세션 노드는 자동 저장 위치가 아니다", wiki_management)
            self.assertIn("active roadmap를 무관한 작업 기록의 fallback", wiki_management)
            collaboration = (
                db_dir / "docs" / "protocols" / "agent-collaboration-guide.md"
            ).read_text(encoding="utf-8")
            self.assertIn("작업별 branch+worktree", collaboration)
            self.assertIn("dirty worktree를 강제로 제거", collaboration)
            self.assertGreater(first["wiki_files_created"], 0)
            self.assertGreater(first["directives_seeded"], 0)
            self.assertEqual(first["directives_updated"], 0)
            self.assertEqual(first["directives_removed"], 0)
            self.assertEqual(second["wiki_files_created"], 0)
            self.assertEqual(second["directives_seeded"], 0)
            self.assertGreater(second["directives_updated"], 0)
            self.assertEqual(second["directives_removed"], 0)

            conn = get_connection(db_dir)
            try:
                rows = conn.execute(
                    """
                    SELECT key, source, trigger_type, enforcement_level,
                           trigger_data, workflow_skill_id, guard_id
                    FROM directives
                    ORDER BY key
                    """
                ).fetchall()
            finally:
                conn.close()
            self.assertTrue(rows)
            self.assertTrue(all(row["source"] == "install-managed" for row in rows))
            self.assertTrue(all(row["trigger_type"] == "always" for row in rows))
            rows_by_key = {row["key"]: row for row in rows}
            self.assertEqual(rows_by_key["task-workflow-dispatch"]["enforcement_level"], "workflow")
            self.assertEqual(
                rows_by_key["task-workflow-dispatch"]["workflow_skill_id"],
                "engram-task-workflow",
            )
            self.assertEqual(rows_by_key["wiki-workflow-dispatch"]["enforcement_level"], "workflow")
            self.assertEqual(
                rows_by_key["session-lifecycle-workflow"]["workflow_skill_id"],
                "engram-close-session",
            )
            self.assertEqual(rows_by_key["persona-voice"]["enforcement_level"], "advisory")
            self.assertEqual(
                json.loads(rows_by_key["task-workflow-dispatch"]["trigger_data"])["actions_any"][0],
                "build",
            )
            self.assertEqual(rows_by_key["protected-branch-guard"]["enforcement_level"], "blocking")
            self.assertEqual(rows_by_key["protected-branch-guard"]["guard_id"], "protected-branch")
            self.assertEqual(
                json.loads(rows_by_key["protected-branch-guard"]["trigger_data"])["action_modes_any"],
                ["repo-write"],
            )
            self.assertFalse(
                json.loads(rows_by_key["protected-branch-guard"]["trigger_data"])["render_in_prompt"]
            )
            self.assertEqual(rows_by_key["dirty-worktree-guard"]["guard_id"], "dirty-worktree")
            self.assertIn(
                "new-independent-task",
                json.loads(rows_by_key["dirty-worktree-guard"]["trigger_data"])["action_tags_any"],
            )

    def test_migrates_unmodified_install_directives_and_preserves_user_edits(self):
        templates = Path(__file__).resolve().parents[1] / "installer" / "templates"
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                bootstrap_install(db_dir, templates)
                conn = get_connection(db_dir)
                try:
                    with conn:
                        conn.execute(
                            "INSERT INTO directives "
                            "(key, content, source, scope, priority, trigger_type) "
                            "VALUES ('wiki-management', 'legacy', 'install', 'all', 5, 'wiki')"
                        )
                        conn.execute(
                            "INSERT INTO directives "
                            "(key, content, source, scope, priority, trigger_type) "
                            "VALUES ('wiki-governance-trigger', 'legacy', 'install', 'all', 10, 'wiki')"
                        )
                        conn.execute(
                            "INSERT INTO directives "
                            "(key, content, source, scope, priority, trigger_type) "
                            "VALUES ('git-branch-policy', 'custom', 'user', 'all', 4, 'always')"
                        )
                    result = bootstrap_install(db_dir, templates)
                    obsolete = conn.execute(
                        "SELECT 1 FROM directives WHERE key = 'wiki-management'"
                    ).fetchone()
                    custom = conn.execute(
                        "SELECT content, source FROM directives WHERE key = 'git-branch-policy'"
                    ).fetchone()
                finally:
                    conn.close()

            self.assertEqual(result["directives_removed"], 2)
            self.assertIsNone(obsolete)
            self.assertEqual(custom["content"], "custom")
            self.assertEqual(custom["source"], "user")

    def test_user_update_detaches_directive_from_installer_management(self):
        templates = Path(__file__).resolve().parents[1] / "installer" / "templates"
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                bootstrap_install(db_dir, templates)
                with patch(
                    "core.context.directives.get_connection",
                    return_value=get_connection(db_dir),
                ):
                    self.assertTrue(
                        update_directive(
                            "task-workflow-dispatch", content="my custom task rule"
                        )
                    )
                bootstrap_install(db_dir, templates)

                conn = get_connection(db_dir)
                try:
                    row = conn.execute(
                        "SELECT content, source FROM directives "
                        "WHERE key = 'task-workflow-dispatch'"
                    ).fetchone()
                finally:
                    conn.close()

            self.assertEqual(row["content"], "my custom task rule")
            self.assertEqual(row["source"], "user")


if __name__ == "__main__":
    unittest.main()
