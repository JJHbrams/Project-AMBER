import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.context.directive_policy import evaluate_directive_policy
from core.context.guard_execution import execute_guard, normalize_chore_intent
from core.context.directives import (
    add_directive,
    list_directive_policy_audit,
    preflight_directives,
)
from core.storage.db import get_connection, initialize_db


class DirectivePolicyTests(unittest.TestCase):
    def test_chore_intent_does_not_treat_false_string_as_true(self):
        self.assertFalse(normalize_chore_intent({"is_chore": "false"})["is_chore"])
        self.assertTrue(normalize_chore_intent({"is_chore": "true"})["is_chore"])

    @patch(
        "core.context.guard_execution.subprocess.run",
        side_effect=PermissionError("git execution denied"),
    )
    def test_guard_returns_structured_error_for_git_os_error(self, _mock_run):
        result = execute_guard(
            "protected-branch",
            cwd=".",
            action_metadata={"mode": "repo-write"},
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["evidence"]["git"]["error_type"],
            "os_error",
        )

    def _git(self, repo: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed\nstdout={result.stdout}\nstderr={result.stderr}"
            )
        return str(result.stdout or "").strip()

    def _init_repo(self, root: Path, branch: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        self._git(root, "init")
        self._git(root, "config", "user.email", "test@example.com")
        self._git(root, "config", "user.name", "Directive Policy Test")
        (root / "README.md").write_text("init\n", encoding="utf-8")
        self._git(root, "add", "README.md")
        self._git(root, "commit", "-m", "init")
        self._git(root, "checkout", "-B", branch)
        return root

    def test_schema_migration_backfills_legacy_directive_policy_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                conn = get_connection(db_dir)
                try:
                    with conn:
                        conn.execute(
                            """
                            CREATE TABLE directives (
                                key        TEXT PRIMARY KEY,
                                content    TEXT NOT NULL,
                                source     TEXT NOT NULL DEFAULT 'unknown',
                                scope      TEXT NOT NULL DEFAULT 'all',
                                priority   INTEGER NOT NULL DEFAULT 0,
                                active     INTEGER NOT NULL DEFAULT 1,
                                created_at TEXT DEFAULT (datetime('now','localtime')),
                                updated_at TEXT DEFAULT (datetime('now','localtime'))
                            )
                            """
                        )
                        conn.execute(
                            """
                            INSERT INTO directives (key, content, source, scope, priority, active)
                            VALUES ('legacy-wiki', 'legacy rule', 'user', 'all', 4, 1)
                            """
                        )
                        conn.execute("ALTER TABLE directives ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'wiki'")
                        conn.execute(
                            "UPDATE directives SET trigger_type = 'wiki' WHERE key = 'legacy-wiki'"
                        )
                finally:
                    conn.close()

                initialize_db(db_dir)

                conn = get_connection(db_dir)
                try:
                    row = conn.execute(
                        """
                        SELECT enforcement_level, trigger_data, workflow_skill_id,
                               guard_id, legacy_migration_markers
                        FROM directives
                        WHERE key = 'legacy-wiki'
                        """
                    ).fetchone()
                    audit_table = conn.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table' AND name = 'directive_policy_audit'
                        """
                    ).fetchone()
                finally:
                    conn.close()

            self.assertIsNotNone(audit_table)
            self.assertEqual(row["enforcement_level"], "advisory")
            self.assertEqual(json.loads(row["trigger_data"]), {"legacy_trigger_types": ["wiki"]})
            self.assertEqual(row["workflow_skill_id"], "")
            self.assertEqual(row["guard_id"], "")
            self.assertIn("legacy-default-advisory", json.loads(row["legacy_migration_markers"]))
            self.assertIn("legacy-trigger:wiki", json.loads(row["legacy_migration_markers"]))

    def test_evaluator_is_deterministic_and_idempotent(self):
        directives = [
            {
                "key": "workflow-task",
                "content": "run task workflow",
                "scope": "all",
                "priority": 5,
                "active": 1,
                "trigger_type": "always",
                "enforcement_level": "workflow",
                "workflow_skill_id": "engram-task-workflow",
                "trigger_data": {"actions_any": ["code"]},
                "created_at": "2026-08-14 12:00:02",
            },
            {
                "key": "always-note",
                "content": "always note",
                "scope": "all",
                "priority": 20,
                "active": 1,
                "trigger_type": "always",
                "enforcement_level": "advisory",
                "trigger_data": {"match": "always"},
                "created_at": "2026-08-14 12:00:00",
            },
            {
                "key": "danger-guard",
                "content": "stop dangerous action",
                "scope": "all",
                "priority": 5,
                "active": 1,
                "trigger_type": "always",
                "enforcement_level": "blocking",
                "guard_id": "dangerous-change",
                "trigger_data": {"query_keywords_any": ["danger"]},
                "created_at": "2026-08-14 12:00:01",
            },
        ]

        first = evaluate_directive_policy(
            reversed(directives),
            caller="all",
            user_query="danger code change",
            action="code edit",
            scope_key="project:engram",
            project_key="engram",
        )
        second = evaluate_directive_policy(
            directives,
            caller="all",
            user_query="danger code change",
            action="code edit",
            scope_key="project:engram",
            project_key="engram",
        )

        self.assertEqual(first, second)
        self.assertEqual(first["decision"], "blocked")
        self.assertEqual(
            [directive["key"] for directive in first["matched_directives"]],
            ["always-note", "danger-guard", "workflow-task"],
        )

    def test_evaluator_returns_allow_workflow_and_blocked(self):
        advisory = evaluate_directive_policy(
            [
                {
                    "key": "always-note",
                    "content": "always note",
                    "scope": "all",
                    "priority": 1,
                    "active": 1,
                    "trigger_type": "always",
                }
            ],
            caller="all",
            user_query="hello",
        )
        workflow = evaluate_directive_policy(
            [
                {
                    "key": "task",
                    "content": "run task workflow",
                    "scope": "all",
                    "priority": 1,
                    "active": 1,
                    "trigger_type": "always",
                    "enforcement_level": "workflow",
                    "workflow_skill_id": "engram-task-workflow",
                    "trigger_data": {"actions_any": ["code"]},
                }
            ],
            caller="all",
            action="code edit",
        )
        blocked = evaluate_directive_policy(
            [
                {
                    "key": "guard",
                    "content": "guard this",
                    "scope": "all",
                    "priority": 1,
                    "active": 1,
                    "trigger_type": "always",
                    "enforcement_level": "blocking",
                    "guard_id": "no-force-push",
                    "trigger_data": {"action_keywords_any": ["push"]},
                }
            ],
            caller="all",
            action="git push",
        )

        self.assertEqual(advisory["decision"], "allow")
        self.assertEqual(advisory["advisory_notes"][0]["directive_key"], "always-note")
        self.assertEqual(workflow["decision"], "workflow_required")
        self.assertEqual(workflow["required_workflows"][0]["workflow_skill_id"], "engram-task-workflow")
        self.assertEqual(blocked["decision"], "blocked")
        self.assertEqual(blocked["blocking_guards"][0]["guard_id"], "no-force-push")

    def test_preflight_persists_audit_and_list_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "task-workflow",
                        "run workflow",
                        source="test",
                        trigger_type="always",
                        enforcement_level="workflow",
                        workflow_skill_id="engram-task-workflow",
                        trigger_data={"actions_any": ["code"]},
                    )

                    first = preflight_directives(action="code edit", persist_audit=True)
                    second = preflight_directives(action="code edit", persist_audit=True)

                    values = {
                        "directives.policy.audit_default_limit": 20,
                        "directives.policy.audit_max_limit": 1,
                    }
                    with patch(
                        "core.context.directives.get_cfg_value",
                        side_effect=lambda key, default=None: values.get(key, default),
                    ):
                        audits = list_directive_policy_audit(limit=50)

            self.assertEqual(first["decision"], "workflow_required")
            self.assertIn("audit_id", second)
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]["id"], second["audit_id"])
            self.assertEqual(audits[0]["decision"], "workflow_required")
            self.assertEqual(
                audits[0]["required_workflows"][0]["workflow_skill_id"],
                "engram-task-workflow",
            )

    def test_preflight_report_only_keeps_blocking_result_without_guard_execution_fields(self):
        with tempfile.TemporaryDirectory() as tmp_repo, tempfile.TemporaryDirectory() as tmp_db:
            repo = self._init_repo(Path(tmp_repo), "feat/report-only")
            db_dir = Path(tmp_db)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "protected-branch-guard",
                        "protect repo writes",
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
                    result = preflight_directives(
                        action="apply patch",
                        cwd=str(repo),
                        action_metadata={"mode": "repo-write"},
                        persist_audit=False,
                    )

        self.assertEqual(result["decision"], "blocked")
        self.assertNotIn("final_status", result)
        self.assertNotIn("executed_guard_results", result)

    def test_protected_branch_guard_allows_feature_branch_repo_write(self):
        with tempfile.TemporaryDirectory() as tmp_repo, tempfile.TemporaryDirectory() as tmp_db:
            repo = self._init_repo(Path(tmp_repo), "feat/guard-pass")
            db_dir = Path(tmp_db)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "protected-branch-guard",
                        "protect repo writes",
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
                    result = preflight_directives(
                        action="apply patch",
                        cwd=str(repo),
                        action_metadata={"mode": "repo-write"},
                        execute_guards=True,
                        persist_audit=False,
                    )

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["final_status"], "allow")
        self.assertEqual(result["executed_guard_results"][0]["status"], "pass")
        self.assertEqual(
            result["executed_guard_results"][0]["evidence"]["branch"],
            "feat/guard-pass",
        )

    def test_protected_branch_guard_blocks_dev_repo_write_without_chore(self):
        with tempfile.TemporaryDirectory() as tmp_repo, tempfile.TemporaryDirectory() as tmp_db:
            repo = self._init_repo(Path(tmp_repo), "dev")
            db_dir = Path(tmp_db)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "protected-branch-guard",
                        "protect repo writes",
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
                    result = preflight_directives(
                        action="apply patch",
                        cwd=str(repo),
                        action_metadata={"mode": "repo-write"},
                        execute_guards=True,
                        persist_audit=False,
                    )

        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["final_status"], "blocked")
        self.assertEqual(result["executed_guard_results"][0]["status"], "fail")

    def test_protected_branch_guard_allows_structured_chore_exception(self):
        with tempfile.TemporaryDirectory() as tmp_repo, tempfile.TemporaryDirectory() as tmp_db:
            repo = self._init_repo(Path(tmp_repo), "dev")
            db_dir = Path(tmp_db)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "protected-branch-guard",
                        "protect repo writes",
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
                    result = preflight_directives(
                        action="apply patch",
                        cwd=str(repo),
                        action_metadata={"mode": "repo-write"},
                        chore_intent={"is_chore": True, "summary": "dependency bump"},
                        execute_guards=True,
                        persist_audit=False,
                    )

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["final_status"], "allow")
        self.assertTrue(result["executed_guard_results"][0]["evidence"]["chore_intent"]["is_chore"])

    def test_dirty_worktree_guard_allows_clean_worktree_for_new_independent_task(self):
        with tempfile.TemporaryDirectory() as tmp_repo, tempfile.TemporaryDirectory() as tmp_db:
            repo = self._init_repo(Path(tmp_repo), "feat/clean-worktree")
            db_dir = Path(tmp_db)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "dirty-worktree-guard",
                        "protect independent tasks from dirty worktrees",
                        source="test",
                        trigger_type="always",
                        enforcement_level="blocking",
                        guard_id="dirty-worktree",
                        trigger_data={
                            "match": "always",
                            "action_modes_any": ["repo-write"],
                            "action_tags_any": ["new-independent-task"],
                            "render_in_prompt": False,
                        },
                    )
                    result = preflight_directives(
                        action="apply patch",
                        cwd=str(repo),
                        action_metadata={
                            "mode": "repo-write",
                            "tags": ["new-independent-task"],
                        },
                        independent_task_context={
                            "requested": True,
                            "existing_changes_owner": "other-task",
                            "existing_task_id": "task-a",
                            "new_task_id": "task-b",
                        },
                        execute_guards=True,
                        persist_audit=False,
                    )

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["final_status"], "allow")
        self.assertEqual(result["executed_guard_results"][0]["status"], "pass")

    def test_dirty_worktree_guard_blocks_dirty_worktree_for_other_task(self):
        with tempfile.TemporaryDirectory() as tmp_repo, tempfile.TemporaryDirectory() as tmp_db:
            repo = self._init_repo(Path(tmp_repo), "feat/dirty-worktree")
            (repo / "notes.txt").write_text("dirty\n", encoding="utf-8")
            db_dir = Path(tmp_db)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "dirty-worktree-guard",
                        "protect independent tasks from dirty worktrees",
                        source="test",
                        trigger_type="always",
                        enforcement_level="blocking",
                        guard_id="dirty-worktree",
                        trigger_data={
                            "match": "always",
                            "action_modes_any": ["repo-write"],
                            "action_tags_any": ["new-independent-task"],
                            "render_in_prompt": False,
                        },
                    )
                    result = preflight_directives(
                        action="apply patch",
                        cwd=str(repo),
                        action_metadata={
                            "mode": "repo-write",
                            "tags": ["new-independent-task"],
                        },
                        independent_task_context={
                            "requested": True,
                            "existing_changes_owner": "other-task",
                            "existing_task_id": "task-a",
                            "new_task_id": "task-b",
                        },
                        execute_guards=True,
                        persist_audit=False,
                    )

        self.assertEqual(result["decision"], "blocked")
        self.assertEqual(result["final_status"], "blocked")
        self.assertEqual(result["executed_guard_results"][0]["status"], "fail")
        self.assertTrue(result["executed_guard_results"][0]["evidence"]["dirty"])

    def test_unknown_guard_fails_closed_only_when_execution_is_requested(self):
        with tempfile.TemporaryDirectory() as tmp_repo, tempfile.TemporaryDirectory() as tmp_db:
            repo = self._init_repo(Path(tmp_repo), "feat/unknown-guard")
            db_dir = Path(tmp_db)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "unknown-guard",
                        "unknown guard",
                        source="test",
                        trigger_type="always",
                        enforcement_level="blocking",
                        guard_id="does-not-exist",
                        trigger_data={"match": "always"},
                    )
                    report_only = preflight_directives(
                        action="apply patch",
                        cwd=str(repo),
                        execute_guards=False,
                        persist_audit=False,
                    )
                    executed = preflight_directives(
                        action="apply patch",
                        cwd=str(repo),
                        execute_guards=True,
                        persist_audit=False,
                    )

        self.assertEqual(report_only["decision"], "blocked")
        self.assertNotIn("final_status", report_only)
        self.assertEqual(executed["decision"], "error")
        self.assertEqual(executed["final_status"], "error")
        self.assertEqual(executed["executed_guard_results"][0]["status"], "error")

    def test_guard_execution_audit_persists_final_status_and_results(self):
        with tempfile.TemporaryDirectory() as tmp_repo, tempfile.TemporaryDirectory() as tmp_db:
            repo = self._init_repo(Path(tmp_repo), "dev")
            db_dir = Path(tmp_db)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                initialize_db(db_dir)
                with patch(
                    "core.context.directives.get_connection",
                    side_effect=lambda: get_connection(db_dir),
                ):
                    add_directive(
                        "protected-branch-guard",
                        "protect repo writes",
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
                    result = preflight_directives(
                        action="apply patch",
                        cwd=str(repo),
                        action_metadata={"mode": "repo-write"},
                        execute_guards=True,
                        persist_audit=True,
                    )
                    audits = list_directive_policy_audit(limit=10)

        self.assertEqual(result["decision"], "blocked")
        self.assertIn("audit_id", result)
        self.assertEqual(audits[0]["id"], result["audit_id"])
        self.assertEqual(audits[0]["cwd"], str(repo))
        self.assertEqual(audits[0]["action_metadata"]["mode"], "repo-write")
        self.assertTrue(audits[0]["execute_guards"])
        self.assertEqual(audits[0]["final_status"], "blocked")
        self.assertEqual(audits[0]["executed_guard_results"][0]["guard_id"], "protected-branch")
        self.assertEqual(audits[0]["executed_guard_results"][0]["status"], "fail")

    def test_initialize_db_is_idempotent_under_concurrent_legacy_directive_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp)
            with patch.dict(os.environ, {"ENGRAM_DB_DIR": str(db_dir)}):
                conn = get_connection(db_dir)
                try:
                    with conn:
                        conn.execute(
                            """
                            CREATE TABLE directives (
                                key        TEXT PRIMARY KEY,
                                content    TEXT NOT NULL,
                                source     TEXT NOT NULL DEFAULT 'unknown',
                                scope      TEXT NOT NULL DEFAULT 'all',
                                priority   INTEGER NOT NULL DEFAULT 0,
                                active     INTEGER NOT NULL DEFAULT 1,
                                created_at TEXT DEFAULT (datetime('now','localtime')),
                                updated_at TEXT DEFAULT (datetime('now','localtime'))
                            )
                            """
                        )
                finally:
                    conn.close()

                barrier = threading.Barrier(4)
                errors: list[BaseException] = []

                def worker():
                    try:
                        barrier.wait()
                        initialize_db(db_dir)
                    except BaseException as exc:
                        errors.append(exc)

                with patch("core.graph.knowledge.initialize_kg_tables", return_value=None):
                    threads = [threading.Thread(target=worker) for _ in range(4)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join()

                self.assertEqual(errors, [])

                conn = get_connection(db_dir)
                try:
                    columns = {
                        row["name"]: row
                        for row in conn.execute("PRAGMA table_info(directives)").fetchall()
                    }
                finally:
                    conn.close()

            self.assertIn("enforcement_level", columns)
            self.assertIn("trigger_data", columns)
            self.assertIn("workflow_skill_id", columns)
            self.assertIn("guard_id", columns)
            self.assertIn("legacy_migration_markers", columns)


if __name__ == "__main__":
    unittest.main()
