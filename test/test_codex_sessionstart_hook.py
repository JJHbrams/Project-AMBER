import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.integrations.engram_bootstrap import _render_hook_script, build_bootstrap_directive


class CodexBootstrapDirectiveTests(unittest.TestCase):
    def test_codex_directive_has_no_toolsearch_step(self):
        directive = build_bootstrap_directive(caller="Codex", cwd="C:/w")

        self.assertNotIn("ToolSearch with query", directive)
        self.assertIn("tools.mcp__engram__engram_get_context_once({caller:'Codex'", directive)
        self.assertIn("cwd:'C:/w'", directive)
        self.assertNotIn('"', directive)

    def test_claude_directive_is_unchanged(self):
        directive = build_bootstrap_directive(cwd="C:/w")

        self.assertIn("ToolSearch with query 'select:mcp__engram__engram_get_context_once'", directive)
        self.assertIn("caller='claude-code'", directive)

    def test_hook_script_contains_both_branches(self):
        script = _render_hook_script()

        self.assertIn("caller:'Codex'", script)
        self.assertIn("caller='claude-code'", script)
        self.assertIn("rollout-", script)


@unittest.skipUnless(shutil.which("powershell"), "PowerShell required")
class CodexSessionStartHookRuntimeTests(unittest.TestCase):
    def _run(self, stdin: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hook.ps1"
            path.write_text(_render_hook_script(), encoding="utf-8")
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_codex_transcript_gets_codex_directive(self):
        payload = {"transcript_path": r"C:\u\.codex\sessions\2026\09\30\rollout-2026-09-30T10-25-52-01a0.jsonl"}
        out = self._run(json.dumps(payload))

        self.assertIn("caller:'Codex'", out)
        self.assertNotIn("ToolSearch with query", out)
        self.assertIn("2-8 word", out)

    def test_claude_transcript_gets_claude_directive(self):
        payload = {"transcript_path": r"C:\u\.claude\projects\p\0a1b.jsonl"}
        out = self._run(json.dumps(payload))

        self.assertIn("caller='claude-code'", out)

    def test_unparseable_stdin_falls_back_to_claude(self):
        self.assertIn("caller='claude-code'", self._run("not-json"))


if __name__ == "__main__":
    unittest.main()
