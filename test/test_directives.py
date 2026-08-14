import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.context.directives import get_directives
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


if __name__ == "__main__":
    unittest.main()
