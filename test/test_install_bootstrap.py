import os
import shutil
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
            user_note = db_dir / "docs" / "notes" / "my-note.md"
            user_note.parent.mkdir(parents=True, exist_ok=True)
            user_note.write_text("my existing wiki", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "ENGRAM_DB_DIR": str(db_dir),
                    "ENGRAM_SMOKE_DB_DIR": str(db_dir),
                },
            ):
                first = bootstrap_install(db_dir, templates)
                second = bootstrap_install(db_dir, templates)

            self.assertEqual(existing.read_text(encoding="utf-8"), "keep me")
            self.assertEqual(user_note.read_text(encoding="utf-8"), "my existing wiki")
            for relative in (
                "docs/moc/000-HOME.md",
                "docs/guides/wiki-guide.md",
                "docs/_templates/concept.md",
                "docs/_templates/project.md",
                "docs/protocols/wiki-management-guide.md",
                "docs/protocols/git-branch-guide.md",
            ):
                self.assertTrue((db_dir / relative).is_file(), relative)
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
            self.assertIn("self-diagnosis-manual-first", rows_by_key)

    def test_manual_pages_are_managed_without_overwriting_other_wiki_or_user_files(self):
        source_templates = Path(__file__).resolve().parents[1] / "installer" / "templates"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            templates = root / "templates"
            shutil.copytree(source_templates, templates)
            db_dir = root / "db"
            user_guide = db_dir / "docs" / "guides" / "wiki-guide.md"
            user_guide.parent.mkdir(parents=True)
            user_guide.write_text("keep this user guide", encoding="utf-8")
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir), "ENGRAM_SMOKE_DB_DIR": str(db_dir)}):
                first = bootstrap_install(db_dir, templates)
                manual_page = db_dir / "docs" / "guides" / "engram-manual" / "index.md"
                manual_page.write_text("old managed content", encoding="utf-8")
                second = bootstrap_install(db_dir, templates)

            self.assertGreater(first["manual_files_created"], 0)
            self.assertGreater(second["manual_files_updated"], 0)
            self.assertNotEqual(manual_page.read_text(encoding="utf-8"), "old managed content")
            self.assertEqual(user_guide.read_text(encoding="utf-8"), "keep this user guide")
            state = json.loads((manual_page.parent / ".install-manifest.json").read_text(encoding="utf-8"))
            self.assertIn("index.md", state["managed_files"])
            self.assertIn("index.md", state["content_hashes"])

    def test_manual_manifest_adds_removes_and_preserves_unlisted_files(self):
        source_templates = Path(__file__).resolve().parents[1] / "installer" / "templates"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            templates = root / "templates"
            shutil.copytree(source_templates, templates)
            db_dir = root / "db"
            manifest_path = templates / "manual" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"].extend(["new-page.md", "stale-page.md"])
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (templates / "manual" / "new-page.md").write_text("new managed", encoding="utf-8")
            (templates / "manual" / "stale-page.md").write_text("stale managed", encoding="utf-8")
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir), "ENGRAM_SMOKE_DB_DIR": str(db_dir)}):
                bootstrap_install(db_dir, templates)
                manual_root = db_dir / "docs" / "guides" / "engram-manual"
                (manual_root / "user-page.md").write_text("preserve me", encoding="utf-8")
                manifest["files"].remove("stale-page.md")
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                result = bootstrap_install(db_dir, templates)

            self.assertTrue((manual_root / "new-page.md").is_file())
            self.assertFalse((manual_root / "stale-page.md").exists())
            self.assertEqual(result["manual_files_removed"], 1)
            self.assertEqual((manual_root / "user-page.md").read_text(encoding="utf-8"), "preserve me")

    def test_legacy_manual_migration_and_unsafe_manifest_rejection(self):
        source_templates = Path(__file__).resolve().parents[1] / "installer" / "templates"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            templates = root / "templates"
            shutil.copytree(source_templates, templates)
            db_dir = root / "db"
            manual_root = db_dir / "docs" / "guides" / "engram-manual"
            manual_root.mkdir(parents=True)
            (manual_root / "tutorial.md").write_text("legacy", encoding="utf-8")
            (manual_root / "custom.md").write_text("user", encoding="utf-8")
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir), "ENGRAM_SMOKE_DB_DIR": str(db_dir)}):
                result = bootstrap_install(db_dir, templates)

            self.assertEqual(result["manual_files_removed"], 1)
            self.assertFalse((manual_root / "tutorial.md").exists())
            self.assertEqual((manual_root / "custom.md").read_text(encoding="utf-8"), "user")
            manifest_path = templates / "manual" / "manifest.json"
            bad_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            bad_manifest["files"] = ["../escape.md"]
            manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir), "ENGRAM_SMOKE_DB_DIR": str(db_dir)}):
                with self.assertRaisesRegex(ValueError, "unsafe manual manifest path"):
                    bootstrap_install(db_dir, templates)

    def test_migrates_unmodified_install_directives_and_preserves_user_edits(self):
        templates = Path(__file__).resolve().parents[1] / "installer" / "templates"
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "ENGRAM_DB_DIR": str(db_dir),
                    "ENGRAM_SMOKE_DB_DIR": str(db_dir),
                },
            ):
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
            with patch.dict(
                os.environ,
                {
                    "ENGRAM_DB_DIR": str(db_dir),
                    "ENGRAM_SMOKE_DB_DIR": str(db_dir),
                },
            ):
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
