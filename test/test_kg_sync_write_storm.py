"""kg sync 가 쓰기 락을 오래 쥐지 않는지 (2026-10-19 kg-watcher 무응답).

- resolve_links 전체 모드가 엣지를 지우고 다시 넣지 않고 차이만 반영한다.
- watcher 디바운서가 싱크를 겹쳐 띄우지 않고, 도중에 온 변경은 끝난 뒤 한 번 더 돈다.
- MCP 서버가 응답만 늦을 때 watcher 가 같은 DB 에 직접 쓰기로 끼어들지 않는다.
"""

import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from core.graph.knowledge.knowledge_graph import KnowledgeGraph
from scripts.kg import kg_watcher


class ResolveLinksDiffTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.docs = Path(self._tmp.name) / "docs"
        self.docs.mkdir()
        self._db = str(Path(self._tmp.name) / "kg.db")

        def gc():
            c = sqlite3.connect(self._db)
            c.row_factory = sqlite3.Row
            return c

        self._patch = patch("core.graph.knowledge.knowledge_graph.get_connection", side_effect=gc)
        self._patch.start()
        self.kg = KnowledgeGraph()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, rel, fm):
        p = self.docs / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\n{fm}\n---\n\n본문\n", encoding="utf-8")

    def _edges(self):
        c = sqlite3.connect(self._db)
        rows = c.execute("SELECT id, from_id, to_id, rel_type FROM kg_edges ORDER BY id").fetchall()
        c.close()
        return rows

    def test_full_resolve_keeps_unchanged_edges(self):
        self._write("a.md", "id: a\ntitle: A\nlinks:\n  - b\n  - c")
        self._write("b.md", "id: b\ntitle: B")
        self._write("c.md", "id: c\ntitle: C")
        self.kg.sync_vault(self.docs)
        before = self._edges()
        self.assertEqual({(r[1], r[2]) for r in before}, {("a", "b"), ("a", "c")})

        self.kg.resolve_links(self.docs)
        self.assertEqual(self._edges(), before)  # 같은 rowid — 지웠다 다시 넣지 않음

    def test_full_resolve_drops_removed_link_only(self):
        self._write("a.md", "id: a\ntitle: A\nlinks:\n  - b\n  - c")
        self._write("b.md", "id: b\ntitle: B")
        self._write("c.md", "id: c\ntitle: C")
        self.kg.sync_vault(self.docs)
        kept = [r for r in self._edges() if r[2] == "b"]

        self._write("a.md", "id: a\ntitle: A\nlinks:\n  - b")
        self.kg.resolve_links(self.docs)
        self.assertEqual(self._edges(), kept)

    def test_manual_edge_survives_full_resolve(self):
        self._write("a.md", "id: a\ntitle: A")
        self._write("b.md", "id: b\ntitle: B")
        self.kg.sync_vault(self.docs)
        self.kg.add_edge("a", "b", "supports", context="사람이 단 엣지")

        self.kg.resolve_links(self.docs)
        self.assertEqual([(r[1], r[2], r[3]) for r in self._edges()], [("a", "b", "supports")])

    def test_restricted_resolve_only_touches_given_file(self):
        self._write("a.md", "id: a\ntitle: A\nlinks:\n  - c")
        self._write("b.md", "id: b\ntitle: B\nlinks:\n  - c")
        self._write("c.md", "id: c\ntitle: C")
        self.kg.sync_vault(self.docs)
        b_edge = [r for r in self._edges() if r[1] == "b"]

        self._write("a.md", "id: a\ntitle: A")
        self.kg.resolve_links(self.docs, restrict_to_paths={"a.md"})
        self.assertEqual(self._edges(), b_edge)

    def test_frontmatter_relation_context_change_is_applied(self):
        rel = "relations:\n  - to: b\n    rel: supports\n    context: {}"
        self._write("a.md", "id: a\ntitle: A\n" + rel.format("처음"))
        self._write("b.md", "id: b\ntitle: B")
        self.kg.sync_vault(self.docs)

        self._write("a.md", "id: a\ntitle: A\n" + rel.format("바뀜"))
        self.kg.resolve_links(self.docs)
        c = sqlite3.connect(self._db)
        rows = c.execute("SELECT rel_type, context FROM kg_edges").fetchall()
        c.close()
        self.assertEqual(rows, [("supports", "[fm] 바뀜")])

    def test_manual_edge_with_same_key_causes_no_write(self):
        self._write("a.md", "id: a\ntitle: A\nrelations:\n  - to: b\n    rel: supports")
        self._write("b.md", "id: b\ntitle: B")
        self.kg.sync_vault(self.docs)
        c = sqlite3.connect(self._db)
        c.execute("UPDATE kg_edges SET context='사람이 고침'")  # 수동 엣지로 바뀜
        c.commit()
        c.close()

        writes = []
        real = self.kg.resolve_links.__func__

        def gc_tracking():
            conn = sqlite3.connect(self._db)
            conn.row_factory = sqlite3.Row
            conn.set_trace_callback(lambda sql: writes.append(sql) if sql.lstrip().upper().startswith(("INSERT", "DELETE")) else None)
            return conn

        with patch("core.graph.knowledge.knowledge_graph.get_connection", side_effect=gc_tracking):
            real(self.kg, self.docs)
        self.assertEqual(writes, [])

    def test_sync_vault_returns_busy_instead_of_waiting_forever(self):
        from core.graph.knowledge import knowledge_graph

        knowledge_graph._SYNC_VAULT_LOCK.acquire()
        try:
            res = self.kg.sync_vault(self.docs, lock_timeout=0.05)
        finally:
            knowledge_graph._SYNC_VAULT_LOCK.release()
        self.assertTrue(res.get("busy"))

    def test_duplicate_id_files_do_not_flip_flop(self):
        self._write("x/index.md", "id: index\ntitle: X")
        self._write("y/index.md", "id: index\ntitle: Y")
        self.kg.sync_vault(self.docs)
        owner = self.kg.get_node("index")["path"]

        second = self.kg.sync_vault(self.docs)
        self.assertEqual(second["synced"], 0)  # 매번 "새 파일"로 잡혀 전체 재해석하지 않는다
        self.assertEqual(self.kg.get_node("index")["path"], owner)

    def test_duplicate_id_taken_over_when_owner_file_changes_id(self):
        self._write("x/index.md", "id: index\ntitle: X")
        self.kg.sync_vault(self.docs)
        self._write("x/index.md", "id: x-index\ntitle: X")
        self._write("y/index.md", "id: index\ntitle: Y")
        self.kg.sync_vault(self.docs)
        self.assertEqual(self.kg.get_node("index")["path"], str(Path("y/index.md")))
        self.assertEqual(self.kg.get_node("x-index")["path"], str(Path("x/index.md")))

    def test_duplicate_file_links_do_not_overwrite_owner_edges(self):
        self._write("t.md", "id: t\ntitle: T")
        self._write("u.md", "id: u\ntitle: U")
        self._write("x/index.md", "id: index\ntitle: X\nlinks:\n  - t")
        self.kg.sync_vault(self.docs)
        self._write("y/index.md", "id: index\ntitle: Y\nlinks:\n  - u")
        self.kg.sync_vault(self.docs)
        self.kg.resolve_links(self.docs)
        self.kg.resolve_links(self.docs, restrict_to_paths={str(Path("y/index.md"))})
        self.assertEqual({(r[1], r[2]) for r in self._edges()}, {("index", "t")})

    def test_cross_vault_duplicate_id_does_not_steal(self):
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        (other / "n.md").write_text("---\nid: shared\ntitle: Other\n---\n", encoding="utf-8")
        self.kg.sync_vault(other)
        self._write("n.md", "id: shared\ntitle: Here")
        self.kg.sync_vault(self.docs)
        self.assertEqual(self.kg.get_node("shared")["title"], "Other")

    def test_case_only_rename_keeps_syncing(self):
        self._write("A.md", "id: a\ntitle: A")
        self.kg.sync_vault(self.docs)
        (self.docs / "A.md").rename(self.docs / "a.md")
        self._write("a.md", "id: a\ntitle: A2")
        self.kg.sync_vault(self.docs)
        self.assertEqual(self.kg.get_node("a")["title"], "A2")

    def test_id_change_moves_manual_edges(self):
        self._write("a.md", "id: old-a\ntitle: A")
        self._write("b.md", "id: b\ntitle: B")
        self.kg.sync_vault(self.docs)
        self.kg.add_edge("old-a", "b", "supports", context="사람이 단 엣지")
        self._write("a.md", "id: new-a\ntitle: A")
        self.kg.sync_vault(self.docs)
        self.assertEqual([(r[1], r[2], r[3]) for r in self._edges()], [("new-a", "b", "supports")])

    def test_id_change_removes_stale_row_for_same_path(self):
        self._write("a.md", "id: old-a\ntitle: A")
        self.kg.sync_vault(self.docs)
        self._write("a.md", "id: new-a\ntitle: A")
        self.kg.sync_vault(self.docs)

        self.assertIsNone(self.kg.get_node("old-a"))
        self.assertEqual(self.kg.sync_vault(self.docs)["synced"], 0)

    def test_sync_vault_skips_unchanged_files(self):
        self._write("a.md", "id: a\ntitle: A")
        first = self.kg.sync_vault(self.docs)
        second = self.kg.sync_vault(self.docs)
        self.assertEqual((first["synced"], second["synced"], second["unchanged"]), (1, 0, 1))


class DebounceSingleFlightTests(unittest.TestCase):
    def test_fire_during_sync_reruns_instead_of_overlapping(self):
        running = 0
        peak = 0
        calls = 0
        started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()

        def slow_sync(_vault):
            nonlocal running, peak, calls
            with lock:
                running += 1
                calls += 1
                peak = max(peak, running)
            started.set()
            release.wait(5)
            with lock:
                running -= 1

        deb = kg_watcher._DebounceSync(Path("."), delay=0)
        with patch.object(kg_watcher, "_do_sync", side_effect=slow_sync):
            first = threading.Thread(target=deb._fire)
            first.start()
            self.assertTrue(started.wait(5))
            for _ in range(3):
                deb._fire()  # 싱크 중 도착한 변경 3건 — 즉시 반환해야 함
            release.set()
            first.join(5)

        self.assertFalse(first.is_alive())
        self.assertEqual(peak, 1)
        self.assertEqual(calls, 2)  # 원래 1번 + 밀린 변경 몰아서 1번


class McpDelegationTests(unittest.TestCase):
    def _run_do_sync(self, exc):
        with patch.object(kg_watcher.urllib.request, "urlopen", side_effect=exc), patch(
            "core.graph.knowledge.get_kg"
        ) as get_kg:
            kg_watcher._do_sync(Path("."))
        return get_kg

    def test_slow_server_does_not_trigger_direct_write(self):
        get_kg = self._run_do_sync(urllib.error.URLError(socket.timeout("timed out")))
        get_kg.assert_not_called()

    def test_read_timeout_does_not_trigger_direct_write(self):
        get_kg = self._run_do_sync(TimeoutError("timed out"))
        get_kg.assert_not_called()

    def test_reset_after_send_does_not_trigger_direct_write(self):
        import http.client

        for exc in (
            http.client.RemoteDisconnected("closed"),
            ConnectionResetError(10054, "reset"),
            urllib.error.URLError(ConnectionResetError(10054, "reset")),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertIsNone(self._sync_result(exc))

    def test_busy_server_result_is_retried_later(self):
        import io
        import json

        body = io.BytesIO(json.dumps({"status": "busy", "busy": True}).encode())
        body.__enter__ = lambda *a: body
        body.__exit__ = lambda *a: None
        with patch.object(kg_watcher.urllib.request, "urlopen", return_value=body):
            self.assertIsNone(kg_watcher._sync_via_mcp())

        deb = kg_watcher._DebounceSync(Path("."), delay=0)
        with patch.object(kg_watcher, "_do_sync", return_value=True), patch.object(
            deb, "schedule"
        ) as schedule:
            deb._fire()
        schedule.assert_called_once_with(kg_watcher._DebounceSync.RETRY_DELAY)
        self.assertFalse(deb._running)

    def test_server_down_falls_back_to_direct_sync(self):
        self.assertIs(
            self._sync_result(urllib.error.URLError(ConnectionRefusedError(10061, "refused"))), False
        )

    def _sync_result(self, exc):
        with patch.object(kg_watcher.urllib.request, "urlopen", side_effect=exc):
            return kg_watcher._sync_via_mcp()


if __name__ == "__main__":
    unittest.main()
