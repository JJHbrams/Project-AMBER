"""Runtime close-path tests using only disposable DB, files, and HTTP transport."""

import asyncio
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

import mcp_server
from core.memory import store
from core.memory.bus import MemorySession
from core.storage import db
from overlay.bubble.stm_bridge import StmBridge
from overlay.stm_server import _STMHandler


class CloseDailyRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_root = self.root / "db"
        self.external = self.root / "obsidian-daily"
        self.external.mkdir()
        self._raw_connection = db.get_connection
        # Do not call initialize_db: its KG bootstrap has a separate configured
        # root.  This is the complete schema touched by these close paths.
        conn = self._raw_connection(self.db_root)
        with conn:
            conn.executescript(
                """
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL,
                    started_at TEXT DEFAULT (datetime('now','localtime')),
                    ended_at TEXT, summary TEXT, journal_provenance TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE session_projects (
                    session_id INTEGER NOT NULL, project_key TEXT NOT NULL,
                    PRIMARY KEY (session_id, project_key)
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL
                );
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER,
                    content TEXT, keywords TEXT, provider TEXT, model TEXT,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                );
                CREATE TABLE keywords (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE);
                CREATE TABLE memory_keywords (
                    memory_id INTEGER NOT NULL, keyword_id INTEGER NOT NULL,
                    PRIMARY KEY (memory_id, keyword_id)
                );
                CREATE TABLE working_memory (
                    scope_key TEXT PRIMARY KEY, summary TEXT, open_intents TEXT,
                    updated_at TEXT, expires_at TEXT
                );
                CREATE TABLE session_checkpoints (
                    session_id INTEGER PRIMARY KEY, last_message_id INTEGER NOT NULL DEFAULT 0,
                    checkpoint_id TEXT NOT NULL DEFAULT '', updated_at TEXT
                );
                CREATE TABLE session_checkpoint_claims (
                    session_id INTEGER NOT NULL, last_message_id INTEGER NOT NULL,
                    claim_id TEXT NOT NULL, status TEXT NOT NULL, claimed_at REAL NOT NULL,
                    PRIMARY KEY (session_id, last_message_id)
                );
                """
            )
        conn.close()
        self.server = HTTPServer(("127.0.0.1", 0), _STMHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.tmp.cleanup()

    def _connection(self, *_args, **_kwargs):
        return self._raw_connection(self.db_root)

    def _create_session(self, session_id: int, scope_key: str, project_key: str = "general", messages=(), journal_provenance: str = ""):
        conn = self._connection()
        with conn:
            conn.execute("INSERT INTO sessions (id, scope_key, journal_provenance) VALUES (?, ?, ?)", (session_id, scope_key, journal_provenance))
            conn.execute(
                "INSERT INTO session_projects (session_id, project_key) VALUES (?, ?)",
                (session_id, project_key),
            )
            for role, content in messages:
                conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)", (session_id, role, content))
        conn.close()

    def _assert_closed_and_written(self, session_id: int, summary: str, open_intents: str, *, external_exists: bool = True, external_has_checkpoint: bool = True, external_marker: str = ""):
        conn = self._connection()
        ended_at = conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (session_id,)).fetchone()["ended_at"]
        conn.close()
        self.assertTrue(ended_at)
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        managed = self.db_root / "docs" / "daily" / f"{day}.md"
        self.assertTrue(managed.exists())
        text = managed.read_text(encoding="utf-8")
        self.assertIn(summary, text)
        if open_intents:
            self.assertIn(open_intents, text)
        self.assertEqual(text.count(f"engram-checkpoint:session-close-{session_id}"), 1)
        external_note = self.external / f"{day}.md"
        self.assertEqual(external_note.exists(), external_exists)
        if external_exists and external_has_checkpoint:
            self.assertIn(summary, external_note.read_text(encoding="utf-8"))
            self.assertEqual(external_note.read_text(encoding="utf-8").count(external_marker or f"engram-journal:session-close-{session_id}"), 1)

    def _runtime_boundaries(self, external_dir: Path):
        """Patch infrastructure only; the close and note coordinators stay real."""
        fake_kg = MagicMock()
        stack = ExitStack()
        factory = self._connection
        stack.enter_context(patch.object(store, "get_connection", side_effect=factory))
        stack.enter_context(patch.object(mcp_server, "get_connection", side_effect=factory))
        stack.enter_context(patch("overlay.stm_server.get_connection", side_effect=factory))
        stack.enter_context(patch("overlay.bubble.stm_bridge.get_connection", side_effect=factory))
        stack.enter_context(patch("core.graph.semantic.stm_promoter.get_connection", side_effect=factory))
        stack.enter_context(patch("core.storage.db.get_connection", side_effect=factory))
        stack.enter_context(patch("core.memory.daily_checkpoint.get_db_root_dir", return_value=str(self.db_root)))
        stack.enter_context(patch("core.memory.daily_checkpoint.get_cfg_value", return_value=str(external_dir)))
        stack.enter_context(patch("overlay.stm_server.get_cfg_value", return_value=str(external_dir)))
        stack.enter_context(patch("overlay.bubble.stm_bridge.get_cfg_value", return_value=str(external_dir)))
        stack.enter_context(patch("core.memory.daily_checkpoint.get_kg", return_value=fake_kg))
        stack.enter_context(patch(
            "core.memory.daily_checkpoint._call_journal_claude",
            return_value='{"title":"세션 개선 기록","background":"저널 품질 개선","work":"외부 일지 렌더러를 개선했다.","result":"보존 검증을 추가했다.","next":""}',
        ))
        stack.enter_context(patch.object(store, "resolve_kg_node_id", return_value=None))
        stack.enter_context(patch("core.context.project_scope.resolve_scope_key", return_value="runtime:mcp"))
        stack.enter_context(patch("core.context.project_scope.resolve_project_key", return_value=""))
        stack.enter_context(patch("core.context.project_scope.resolve_kg_node_id", return_value=None))
        stack.enter_context(patch.object(mcp_server, "_STM_BASE_URL", f"http://127.0.0.1:{self.server.server_port}"))
        # These are asynchronous enrichment after the durable close, not part of
        # the close/daily-note path under test.
        stack.enter_context(patch("core.memory.save_memory"))
        stack.enter_context(patch.object(mcp_server, "mark_session_continuity_saved"))
        stack.enter_context(patch.object(mcp_server, "_call_log", MagicMock()))
        stack.enter_context(patch("core.graph.semantic.maybe_promote", return_value=False))
        stack.enter_context(patch("core.graph.semantic.stm_promoter.maybe_promote", return_value=False))
        stack.enter_context(patch("overlay.bubble.stm_bridge.flag_reflection_event_from_recent_session"))
        stack.enter_context(patch("core.graph.semantic.update_working_memory_from_recent_session"))
        stack.enter_context(patch("core.graph.semantic.flag_reflection_event_from_recent_session"))
        return stack

    def test_mcp_close_reaches_real_http_handler_and_real_daily_coordinator(self):
        self._create_session(101, "runtime:mcp", project_key="Journal Project")
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        original = "---\naliases: [daily]\n---\n# To do list\n- [ ] 기존 할 일\n\n# ProjectIntelContunuum\n기존 본문\n\n# 다른 프로젝트\n다른 본문\n"
        (self.external / f"{day}.md").write_text(original, encoding="utf-8")
        with self._runtime_boundaries(self.external):
            result = asyncio.run(
                mcp_server.engram_close_session(
                    summary="MCP HTTP 종료 요약",
                    progress="MCP 구현과 검증을 완료했다",
                    open_intents="MCP 다음 작업",
                    scope_key="runtime:mcp",
                    session_id=101,
                    cwd="C:/Users/jhjang/vault623/workspace/projects/ProjectIntelContunuum",
                    trigger_sync=False,
                )
            )

        self.assertEqual(result["status"], "ok")
        # trigger_sync=False is the internal/subagent contract: it requires an
        # exact session but must not write the user's external journal.
        self._assert_closed_and_written(101, "MCP HTTP 종료 요약", "MCP 다음 작업", external_exists=True, external_has_checkpoint=False)
        self.assertEqual((self.external / f"{day}.md").read_text(encoding="utf-8"), original)

    def test_bubble_close_uses_real_coordinator_and_existing_external_daily_note(self):
        transcript = [
            ("user", "일일 저널이 사람이 읽기 쉽게 남도록 외부 기록을 개선해줘."),
            ("assistant", "외부 저널 렌더러를 분리하고 보존 테스트를 추가했다."),
        ]
        self._create_session(202, "runtime:bubble", project_key="Bubble Project", messages=transcript, journal_provenance="bubble")
        bridge = StmBridge(scope_key="runtime:bubble")
        bridge._session = MemorySession(session_id=202, scope_key="runtime:bubble")
        with self._runtime_boundaries(self.external):
            bridge.close("[watchdog] 무시되어야 하는 내부 요약")

        self.assertIsNone(bridge._session)
        self._assert_closed_and_written(202, "보존 검증을 추가했다.", "", external_marker="engram-external-project-v2:")
        text = (self.external / f"{datetime.now().astimezone().strftime('%Y-%m-%d')}.md").read_text(encoding="utf-8")
        self.assertNotIn("watchdog", text)
        self.assertIn("<!-- engram-external-snapshot-v2:start -->", text)
        self.assertIn("- summary: 보존 검증을 추가했다.", text)

    def test_http_root_summarize_uses_configured_external_dir_once_and_keeps_open(self):
        self._create_session(250, "runtime:http", project_key="HTTP Project", messages=[("user", "HTTP 중간 정리")], journal_provenance="root_bootstrap")
        payload = json.dumps({
            "session_id": 250, "scope_key": "runtime:http", "summary": "HTTP 중간 요약",
            "journal_origin": "root",
        }).encode("utf-8")
        request = Request(
            f"http://127.0.0.1:{self.server.server_port}/stm/session/summarize",
            data=payload, headers={"Content-Type": "application/json"}, method="POST",
        )
        with self._runtime_boundaries(self.external):
            with urlopen(request, timeout=2) as response:
                first = json.loads(response.read().decode("utf-8"))
            with urlopen(request, timeout=2) as response:
                second = json.loads(response.read().decode("utf-8"))

        self.assertEqual(first["status"], "checkpointed")
        self.assertEqual(second["status"], "no_new_messages")
        conn = self._connection()
        self.assertIsNone(conn.execute("SELECT ended_at FROM sessions WHERE id=250").fetchone()["ended_at"])
        conn.close()
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        external = (self.external / f"{day}.md").read_text(encoding="utf-8")
        self.assertIn("HTTP 중간 요약", external)
        self.assertEqual(external.count("engram-external-project-v2:"), 1)

    def test_missing_external_directory_never_appears_but_managed_daily_note_does(self):
        missing_external = self.root / "missing" / "daily"
        transcript = [("user", "실제 작업 내용을 기록해줘."), ("assistant", "외부 경로가 없어도 위키 기록을 남겼다.")]
        self._create_session(303, "runtime:missing", messages=transcript)
        bridge = StmBridge(scope_key="runtime:missing")
        bridge._session = MemorySession(session_id=303, scope_key="runtime:missing")
        with self._runtime_boundaries(missing_external):
            bridge.close("")

        self._assert_closed_and_written(303, "보존 검증을 추가했다.", "", external_exists=False)
        self.assertFalse(missing_external.exists())

    def test_empty_automatic_session_closes_without_any_daily_entry(self):
        self._create_session(404, "runtime:empty", messages=[("system", "internal only")])
        bridge = StmBridge(scope_key="runtime:empty")
        bridge._session = MemorySession(session_id=404, scope_key="runtime:empty")
        with self._runtime_boundaries(self.external):
            bridge.close("[watchdog] PID 123")
        conn = self._connection()
        self.assertTrue(conn.execute("SELECT ended_at FROM sessions WHERE id = 404").fetchone()["ended_at"])
        conn.close()
        self.assertFalse((self.db_root / "docs" / "daily").exists())
        self.assertFalse(list(self.external.glob("*.md")))


if __name__ == "__main__":
    unittest.main()
