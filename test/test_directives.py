import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.context.directives import add_directive, get_directive, get_directives, render_directives_prompt, update_directive
from core.storage.db import get_connection, initialize_db


class DirectiveSelectionTests(unittest.TestCase):
    def test_configured_dispatchers_survive_item_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                conn = get_connection(db_dir)
                try:
                    with conn:
                        for index in range(5):
                            conn.execute(
                                "INSERT INTO directives "
                                "(key, content, source, scope, priority, trigger_type) "
                                "VALUES (?, ?, 'test', 'all', ?, 'always')",
                                (f"ordinary-{index}", f"ordinary {index}", 100 - index),
                            )
                        conn.execute(
                            "INSERT INTO directives "
                            "(key, content, source, scope, priority, trigger_type) "
                            "VALUES ('task-workflow-dispatch', 'run task skill', "
                            "'test', 'all', 1, 'always')"
                        )
                finally:
                    conn.close()

                values = {
                    "directives.enforcement.mode": "hybrid",
                    "directives.enforcement.pin_top_n": 0,
                    "directives.enforcement.max_items": 2,
                    "directives.enforcement.pinned_keys": [
                        "task-workflow-dispatch"
                    ],
                }
                with patch(
                    "core.context.directives.get_cfg_value",
                    side_effect=lambda key, default=None: values.get(key, default),
                ):
                    with patch(
                        "core.context.directives.get_connection",
                        side_effect=lambda: get_connection(db_dir),
                    ):
                        result = get_directives()

        self.assertEqual(len(result), 2)
        self.assertIn(
            "task-workflow-dispatch",
            {directive["key"] for directive in result},
        )

    def test_legacy_crud_defaults_to_advisory_and_preserves_structured_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    created = add_directive(
                        "legacy-wiki",
                        "legacy wiki rule",
                        source="test",
                        trigger_type="wiki",
                    )
                    self.assertEqual(created["enforcement_level"], "advisory")
                    self.assertEqual(created["trigger_data"], {"legacy_trigger_types": ["wiki"]})

                    self.assertTrue(update_directive("legacy-wiki", content="updated legacy wiki rule"))
                    stored = get_directive("legacy-wiki")

            self.assertIsNotNone(stored)
            self.assertEqual(stored["content"], "updated legacy wiki rule")
            self.assertEqual(stored["enforcement_level"], "advisory")
            self.assertEqual(stored["trigger_data"], {"legacy_trigger_types": ["wiki"]})
            self.assertEqual(stored["source"], "user")

    def test_render_exposes_workflow_policy_suffix_without_breaking_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "task-workflow-dispatch",
                        "run task workflow",
                        source="test",
                        trigger_type="always",
                        enforcement_level="workflow",
                        workflow_skill_id="engram-task-workflow",
                        trigger_data={"actions_any": ["code"]},
                    )
                    with patch(
                        "core.context.directives.get_cfg_value",
                        side_effect=lambda key, default=None: {
                            "directives.enforcement.mode": "always",
                            "directives.enforcement.max_items": 8,
                            "directives.enforcement.pinned_keys": [],
                        }.get(key, default),
                    ):
                        rendered = render_directives_prompt()

        self.assertIn("run task workflow", rendered)
        self.assertIn("[workflow:engram-task-workflow]", rendered)

    def test_render_skips_preflight_only_guard_directives(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "protected-branch-guard",
                        "guard content should stay out of prompts",
                        source="test",
                        trigger_type="always",
                        enforcement_level="blocking",
                        guard_id="protected-branch",
                        trigger_data={
                            "match": "always",
                            "action_modes_any": ["repo-write"],
                            "render_in_prompt": False,
                        },
                    )
                    with patch(
                        "core.context.directives.get_cfg_value",
                        side_effect=lambda key, default=None: {
                            "directives.enforcement.mode": "always",
                            "directives.enforcement.max_items": 8,
                            "directives.enforcement.pinned_keys": [],
                        }.get(key, default),
                    ):
                        rendered = render_directives_prompt()

        self.assertNotIn("guard content should stay out of prompts", rendered)

    def test_update_directive_regenerates_legacy_trigger_data_from_always_to_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "legacy-always",
                        "legacy always rule",
                        source="test",
                        trigger_type="always",
                    )

                    self.assertTrue(update_directive("legacy-always", trigger_type="git"))
                    stored = get_directive("legacy-always")

        self.assertIsNotNone(stored)
        self.assertEqual(stored["trigger_type"], "git")
        self.assertEqual(stored["trigger_data"], {"legacy_trigger_types": ["git"]})

    def test_update_directive_regenerates_legacy_trigger_data_from_wiki_to_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "legacy-wiki",
                        "legacy wiki rule",
                        source="test",
                        trigger_type="wiki",
                    )

                    self.assertTrue(update_directive("legacy-wiki", trigger_type="git"))
                    stored = get_directive("legacy-wiki")

        self.assertIsNotNone(stored)
        self.assertEqual(stored["trigger_type"], "git")
        self.assertEqual(stored["trigger_data"], {"legacy_trigger_types": ["git"]})


if __name__ == "__main__":
    unittest.main()
