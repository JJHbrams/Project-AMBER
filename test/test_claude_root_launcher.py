"""Contract tests for the exact-owner Claude CLI launcher."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.integrations import claude_root_launcher
from core.storage import db

ROOT = Path(__file__).resolve().parents[1]


class ClaudeRootLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw_connection = db.get_connection
        conn = self.raw_connection(self.root)
        with conn:
            conn.executescript(
                """
                CREATE TABLE root_cli_owners (
                    client_token TEXT PRIMARY KEY, pid INTEGER, creation_identity TEXT,
                    started_at REAL, ended_at REAL, status TEXT, session_id INTEGER
                );
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY, scope_key TEXT NOT NULL DEFAULT '', root_client_token TEXT, ended_at TEXT
                );
                """
            )
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def _connection(self):
        return self.raw_connection(self.root)

    def test_rewrites_actual_prompt_and_closes_only_exact_bound_session(self):
        child = MagicMock(pid=919)
        child.wait.return_value = 0

        def bind_during_wait():
            conn = self._connection()
            with conn:
                conn.execute("INSERT INTO sessions(id,scope_key,root_client_token) VALUES(77,'runtime:root-launcher-bound','token')")
                conn.execute("UPDATE root_cli_owners SET session_id=77 WHERE client_token='token'")
            conn.close()
            return 0

        child.wait.side_effect = bind_during_wait
        response = MagicMock()
        response.__enter__.return_value = response
        with patch.object(claude_root_launcher, "get_connection", side_effect=self._connection), patch.object(
            claude_root_launcher.secrets, "token_urlsafe", return_value="token"
        ), patch.object(claude_root_launcher.subprocess, "Popen", return_value=child) as popen, patch.object(
            claude_root_launcher, "_process_creation_identity", return_value="created"
        ), patch.object(claude_root_launcher.urllib.request, "urlopen", return_value=response) as urlopen:
            result = claude_root_launcher.launch_root_claude(
                ["claude", "--append-system-prompt", "Use engram_get_context_once(caller='Claude', scope_key='overlay')"],
                cwd=str(self.root),
                bootstrap="Use engram_get_context_once(caller='Claude', scope_key='overlay')",
            )

        self.assertEqual(result, 0)
        child_argv = popen.call_args.args[0]
        self.assertIn("client_token='token'", child_argv[2])
        self.assertIn("client_token='token'", popen.call_args.kwargs["env"]["ENGRAM_BOOTSTRAP"])
        body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["session_id"], 77)
        self.assertEqual(body["scope_key"], "runtime:root-launcher-bound")
        self.assertEqual(body["journal_origin"], "root")
        conn = self._connection()
        owner = conn.execute(
            "SELECT pid,creation_identity,status,session_id FROM root_cli_owners WHERE client_token='token'"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(owner), (919, "created", "closed", 77))

    def test_unbound_exit_never_uses_http_scope_fallback(self):
        child = MagicMock(pid=920)
        child.wait.return_value = 0
        with patch.object(claude_root_launcher, "get_connection", side_effect=self._connection), patch.object(
            claude_root_launcher.secrets, "token_urlsafe", return_value="unbound"
        ), patch.object(claude_root_launcher.subprocess, "Popen", return_value=child), patch.object(
            claude_root_launcher, "_process_creation_identity", return_value="created"
        ), patch.object(claude_root_launcher.urllib.request, "urlopen") as urlopen:
            self.assertEqual(claude_root_launcher.launch_root_claude(["claude"], cwd=str(self.root), bootstrap=""), 0)
        urlopen.assert_not_called()
        conn = self._connection()
        status = conn.execute(
            "SELECT status FROM root_cli_owners WHERE client_token='unbound'"
        ).fetchone()["status"]
        conn.close()
        self.assertEqual(status, "exited_unbound")

    def test_generated_claude_shim_uses_frozen_root_launcher(self):
        source = (ROOT / "installer" / "modules" / "07_shims.ps1").read_text(encoding="utf-8")
        claude = source[source.index("$claudeShimLines"):source.index("Write-Ok $ClaudeShimPath")]
        self.assertIn("ENGRAM_ROOT_LAUNCHER=$ProjectRoot\\dist\\engram-overlay\\engram-overlay.exe", claude)
        self.assertIn("--role claude-root-launcher claude", claude)
        self.assertIn("frozen launcher missing", claude)

    def test_overlay_never_starts_global_claude_process_scanner(self):
        source = (ROOT / "overlay" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("target=self._claude_code_watchdog_loop", source)


if __name__ == "__main__":
    unittest.main()
