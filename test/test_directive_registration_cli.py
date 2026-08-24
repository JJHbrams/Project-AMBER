import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "directives" / "register_directive.py"


class DirectiveRegistrationCliTests(unittest.TestCase):
    def _run(self, db_dir: Path, values: list[str]) -> subprocess.CompletedProcess[str]:
        profile = db_dir / "profile"
        profile.mkdir()
        env = os.environ.copy()
        env.update({
            "ENGRAM_SMOKE_DB_DIR": str(db_dir),
            "USERPROFILE": str(profile),
            "HOME": str(profile),
            "APPDATA": str(profile / "AppData"),
            "PYTHONUTF8": "1",
        })
        return subprocess.run(
            [sys.executable, str(SCRIPT)], input="\n".join(values) + "\n",
            text=True, encoding="utf-8", errors="replace", cwd=ROOT,
            env=env, capture_output=True, check=False,
        )

    def _count(self, db_dir: Path, key: str) -> int:
        conn = sqlite3.connect(db_dir / "engram.db")
        try:
            return conn.execute("SELECT COUNT(*) FROM directives WHERE key = ?", (key,)).fetchone()[0]
        finally:
            conn.close()

    def test_isolated_cli_rejection_and_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rejected_db = root / "reject-db"
            rejected_db.mkdir()
            rejected = self._run(rejected_db, [
                "cli-rejected", "Do not store.", "user", "9", "1", "0", "3", "2", "1", "1", "no",
            ])
            self.assertEqual(rejected.returncode, 0, rejected.stderr)
            self.assertIn("Invalid selection", rejected.stdout)
            self.assertIn("not approved; nothing was stored", rejected.stdout)
            self.assertEqual(self._count(rejected_db, "cli-rejected"), 0)

            approved_db = root / "approved-db"
            approved_db.mkdir()
            approved = self._run(approved_db, [
                "cli-approved", "Store this.", "user", "1", "0", "3", "2", "1", "1", "yes",
            ])
            self.assertEqual(approved.returncode, 0, approved.stderr)
            self.assertIn('"status": "directive_committed"', approved.stdout)
            self.assertEqual(self._count(approved_db, "cli-approved"), 1)
