import json
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with patch("core.storage.db.initialize_db"), patch.object(urllib.request, "urlopen", side_effect=OSError("isolated test")):
    import mcp_server as server


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _ScanConnection:
    def __init__(self, ids):
        self.ids = ids

    def execute(self, query, params=None):
        return _Rows([(memory_id,) for memory_id in self.ids])

    def close(self):
        pass


class _MemoryConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, query, params):
        checkpoint, limit = params
        return _Rows([row for row in self.rows if row[0] > checkpoint][:limit])

    def close(self):
        pass


class _SemanticGraph:
    def __init__(self, report, fail_id=None, stale_embeddings=0):
        self.report = report
        self.fail_id = fail_id
        self.stale_embeddings = stale_embeddings
        self.upserted = []

    async def reconcile_episodes(self, canonical_ids, apply=False):
        return self.report

    async def embedding_staleness(self):
        return {
            "model": "intfloat/multilingual-e5-small",
            "kg_nodes": 0,
            "episodes": self.stale_embeddings,
        }

    async def upsert_episode(self, **kwargs):
        episode_id = int(kwargs["episode_id"])
        self.upserted.append(episode_id)
        return episode_id != self.fail_id


def _report(missing=(), stale=(), count=0):
    return {
        "missing_ids": [str(value) for value in missing],
        "stale_ids": [str(value) for value in stale],
        "unlinked_episode_ids": [],
        "episode_count_before": count,
    }


class SyncReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        self.root_patch = patch.object(server, "get_db_root_dir", return_value=self.root)
        self.root_patch.start()
        server._SYNC_STATUS = None
        server._post_session_sync_running.clear()
        server._sync_gate.clear()
        server._sync_cancel.clear()
        server._last_sync_completed_at = 0.0
        server._active_sync_run_id = None

    def tearDown(self):
        server._post_session_sync_running.clear()
        server._sync_gate.clear()
        server._sync_cancel.clear()
        server._SYNC_STATUS = None
        server._active_sync_run_id = None
        self.root_patch.stop()
        self.temp.cleanup()

    def _write_checkpoint(self, value):
        server._save_memories_sync_checkpoint(value)

    async def test_atomic_status_lifecycle_persists(self):
        scheduled = server._save_sync_status(
            {
                **server._empty_sync_status(),
                "state": "scheduled",
                "run_id": "run-1",
            }
        )
        server._update_sync_status(state="running", current_stage="vault_scan")
        server._update_sync_status(state="success", current_stage=None, finished_at=server._now_iso())

        server._SYNC_STATUS = None
        loaded = server._load_sync_status()
        self.assertEqual(scheduled["run_id"], "run-1")
        self.assertEqual(loaded["state"], "success")
        self.assertEqual(loaded["run_id"], "run-1")
        self.assertIsNotNone(loaded["finished_at"])
        self.assertEqual(list(Path(self.root, "temp").glob("sync_status.json.*.tmp")), [])
        with open(Path(self.root, "temp", "sync_status.json"), encoding="utf-8") as status_file:
            self.assertEqual(json.load(status_file)["state"], "success")

    async def test_manual_memories_sync_refreshes_persistent_status(self):
        scan = {
            "checkpoint_id": 77,
            "saved_checkpoint_id": 75,
            "sqlite_max_memory_id": 77,
            "kuzu_episode_count": 77,
            "drift": {
                "missing_ids": [],
                "stale_ids": [],
                "unlinked_episode_ids": [],
            },
        }
        with (
            patch.object(
                server,
                "_memories_sync_impl",
                return_value={"status": "ok", "failed": 0, "total": 77},
            ),
            patch.object(server, "get_semantic_graph", return_value=object()),
            patch.object(server, "_scan_memory_drift", return_value=scan),
        ):
            result = await server.memories_sync()

        status = server._load_sync_status()
        self.assertEqual(result["sync_status"]["checkpoint_id"], 77)
        self.assertEqual(status["state"], "success")
        self.assertEqual(status["checkpoint_id"], 77)
        self.assertEqual(status["sqlite_max_memory_id"], 77)
        self.assertEqual(status["kuzu_episode_count"], 77)

    async def test_interrupted_persisted_run_is_recovered_as_failure(self):
        server._save_sync_status(
            {
                **server._empty_sync_status(),
                "state": "running",
                "run_id": "dead-run",
                "current_stage": "kuzu_kg_sync",
            }
        )
        server._SYNC_STATUS = None

        loaded = server._load_sync_status()

        self.assertEqual(loaded["state"], "failed")
        self.assertEqual(loaded["last_error_stage"], "kuzu_kg_sync")
        self.assertIn("interrupted", loaded["last_error"])
        self.assertIsNotNone(loaded["finished_at"])

    async def test_failed_stage_is_recorded(self):
        Path(self.root, "docs").mkdir()
        server._sync_gate.set()

        class _InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        with (
            patch.object(server.threading, "Thread", _InlineThread),
            patch.object(server, "get_cfg_value", return_value=0),
            patch("core.graph.knowledge.iter_wiki_md_files", side_effect=RuntimeError("scan failed")),
        ):
            result = server._schedule_post_session_sync()

        status = server._load_sync_status()
        self.assertTrue(result["scheduled"])
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["last_error_stage"], "vault_scan")
        self.assertEqual(status["stages"]["vault_scan"]["status"], "failed")
        self.assertIn("scan failed", status["stages"]["vault_scan"]["error"])

    async def test_checkpoint_behind_complete_kuzu_advances_to_max(self):
        self._write_checkpoint(47)
        sg = _SemanticGraph(_report(count=74))
        with patch.object(server, "get_connection", return_value=_ScanConnection(range(1, 75))):
            result = await server._scan_memory_drift(sg)
        self.assertEqual(result["checkpoint_id"], 74)
        self.assertEqual(server._load_memories_sync_checkpoint(), 74)

    async def test_missing_ids_rewind_to_before_first_missing(self):
        self._write_checkpoint(74)
        sg = _SemanticGraph(_report(missing=(48, 63), count=72))
        with patch.object(server, "get_connection", return_value=_ScanConnection(range(1, 75))):
            result = await server._scan_memory_drift(sg)
        self.assertEqual(result["checkpoint_id"], 47)
        self.assertEqual(result["drift"]["missing_ids"], ["48", "63"])

    async def test_stale_embedding_model_rewinds_full_memory_index(self):
        self._write_checkpoint(74)
        sg = _SemanticGraph(_report(count=74), stale_embeddings=74)
        with patch.object(server, "get_connection", return_value=_ScanConnection(range(1, 75))):
            result = await server._scan_memory_drift(sg)
        self.assertEqual(result["checkpoint_id"], 0)
        self.assertEqual(result["drift"]["stale_embedding_episodes"], 74)

    async def test_checkpoint_ahead_is_clamped(self):
        self._write_checkpoint(100)
        sg = _SemanticGraph(_report(count=3))
        with patch.object(server, "get_connection", return_value=_ScanConnection((1, 2, 3))):
            result = await server._scan_memory_drift(sg)
        self.assertEqual(result["checkpoint_id"], 3)
        self.assertEqual(server._load_memories_sync_checkpoint(), 3)

    async def test_upsert_failure_does_not_advance_past_failed_row(self):
        self._write_checkpoint(9)
        rows = [
            (10, 1, "ten", "tag", "2026-01-01T00:00:00+00:00"),
            (11, 1, "eleven", "tag", "2026-01-01T00:00:00+00:00"),
        ]
        sg = _SemanticGraph(_report(missing=(10, 11), count=0), fail_id=11)
        connections = [_ScanConnection((10, 11)), _MemoryConnection(rows)]
        with patch.object(server, "get_connection", side_effect=connections):
            result = await server._sync_memories_incremental_async(sg)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_memory_id"], 11)
        self.assertEqual(result["checkpoint_id"], 10)
        self.assertEqual(server._load_memories_sync_checkpoint(), 10)

    async def test_scheduling_skip_reasons_have_close_session_compatible_shape(self):
        server._save_sync_status(
            {
                **server._empty_sync_status(),
                "state": "failed",
                "last_error_stage": "episode_sync",
                "last_error": "preserve me",
            }
        )
        gate_result = server._schedule_post_session_sync()
        self.assertEqual(gate_result, {"scheduled": False, "reason": "gate_closed"})
        status = server._load_sync_status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["last_error"], "preserve me")
        self.assertEqual(status["last_schedule"]["reason"], "gate_closed")

        server._sync_gate.set()
        server._post_session_sync_running.set()
        with patch.object(server, "get_cfg_value", return_value=0):
            running_result = server._schedule_post_session_sync()
        self.assertEqual(running_result, {"scheduled": False, "reason": "already_running"})
        self.assertIn("scheduled", running_result)
        self.assertIn("reason", running_result)

    async def test_concurrent_schedule_only_starts_one_worker(self):
        server._sync_gate.set()
        release = threading.Event()
        started = threading.Event()

        class _BlockingThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                started.set()

        with (
            patch.object(server, "threading", types.SimpleNamespace(Thread=_BlockingThread)),
            patch.object(server, "get_cfg_value", return_value=0),
        ):
            results = []

            def schedule():
                results.append(server._schedule_post_session_sync())
                release.wait(1)

            threads = [threading.Thread(target=schedule) for _ in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(started.wait(1))
            release.set()
            for thread in threads:
                thread.join()

        self.assertEqual(sum(result["scheduled"] for result in results), 1)
        self.assertEqual({result["reason"] for result in results}, {"scheduled", "gate_closed"})

    async def test_close_session_preserves_source_session_id_in_memory(self):
        with (
            patch("core.context.project_scope.resolve_scope_key", return_value="project:e2e"),
            patch("core.context.project_scope.resolve_project_key", return_value="ProjectE2E"),
            patch("core.context.project_scope.resolve_kg_node_id", return_value=None),
            patch.object(server, "_unique_open_session_id", return_value=("321", False)),
            patch.object(server, "_session_is_open", return_value=True),
            patch.object(server, "_session_is_ended", return_value=True),
            patch.object(server, "engram_summarize_session", return_value={"status": "checkpointed", "session_id": 321}) as summarize,
            patch.object(server, "_stm_post", return_value={"status": "ok", "closed_session_id": 321}) as stm_post,
            patch.object(server, "_apply_autonomous_reflection", return_value=False),
            patch.object(server, "mark_session_continuity_saved"),
        ):
            result = await server.engram_close_session(
                summary="실제 세션 요약",
                open_intents="배포 확인",
                scope_key="project:e2e",
                session_id=321,
                trigger_sync=False,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(summarize.call_args.kwargs["session_id"], 321)
        self.assertEqual(stm_post.call_args.args[0], "/stm/session/close")
        close_payload = stm_post.call_args.args[1]
        self.assertEqual(close_payload["session_id"], 321)
        self.assertEqual(close_payload["scope_key"], "project:e2e")
        self.assertEqual(close_payload["summary"], "실제 세션 요약")
        self.assertEqual(close_payload["open_intents"], "배포 확인")


if __name__ == "__main__":
    unittest.main()
