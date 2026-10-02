import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
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


class CodexHookRendererTests(unittest.TestCase):
    def test_codex_shim_first_prompt_is_conditional(self):
        shim = (Path(__file__).resolve().parents[1] / "installer" / "modules" / "07_shims.ps1").read_text(
            encoding="utf-8")
        line = next(ln for ln in shim.splitlines() if "caller='Codex'" in ln and "ENGRAM_BOOTSTRAP" in ln)

        self.assertIn("already injected at session start", line)
        self.assertIn("do NOT call engram_get_context_once", line)

    def test_codex_directive_defers_to_already_injected_context(self):
        directive = build_bootstrap_directive(caller="Codex", cwd="C:/w")

        self.assertIn("already injected", directive)
        self.assertIn("do NOT call engram_get_context_once", directive)

    def test_codex_branch_fetches_live_context_over_mcp_http(self):
        script = _render_hook_script()

        for needle in (
            "engram_get_context_once", "native_session_id", "notifications/initialized",
            "Mcp-Session-Id", "'DELETE'", "text/event-stream", "mcp_servers", "--upstream-url",
            "17385", "CODEX_HOME", "Stopwatch", "Limit-Context", "Expect100Continue",
            "-OperationTimeoutSec 1",
        ):
            self.assertIn(needle, script)

    def test_codex_branch_has_empty_identity_guard_and_silent_exits(self):
        script = _render_hook_script()

        self.assertIn("IDENTITY_NAME_UNSET|PERSONA_UNINITIALIZED", script)
        self.assertIn("enabled", script)
        self.assertIn("mcp_servers\\.[\"'']?engram", script)  # CLI override on the codex command line
        self.assertGreaterEqual(script.count("exit 0"), 6)

    def test_codex_output_first_line_is_plain_text_not_json_looking(self):
        # Codex drops hook stdout that starts with '[' or '{' (parsed as JSON, fails) - measured.
        from core.integrations.engram_bootstrap import CODEX_HOOK_LOADED_LINE

        self.assertNotIn(CODEX_HOOK_LOADED_LINE[0], "[{")
        self.assertIn("do NOT call".lower(), CODEX_HOOK_LOADED_LINE.lower())
        self.assertIn("engram_get_context_once", CODEX_HOOK_LOADED_LINE)

    def test_codex_branch_script_is_ascii_for_powershell_51(self):
        body = _render_hook_script().split("\n", 2)[2]  # skip the Korean header comment

        self.assertTrue(all(ord(ch) < 128 for ch in body))

    def test_claude_branch_is_byte_identical(self):
        script = _render_hook_script()
        claude_line = f'    Write-Output "{build_bootstrap_directive(cwd="$dir")}"\n'

        self.assertTrue(script.endswith("} else {\n" + claude_line + "}\n"))


class _FakeMcp(BaseHTTPRequestHandler):
    calls: list = []
    context_text = ""
    deleted = False

    def log_message(self, *args):
        pass

    def _reply(self, obj, headers=None, sse=True):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if sse:
            data = b"event: message\ndata: " + data + b"\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        type(self).calls.append(body)
        if body.get("method") == "initialize":
            self._reply({"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}},
                        {"Mcp-Session-Id": "sess-1"})
        elif body.get("method") == "tools/call":
            assert self.headers["Mcp-Session-Id"] == "sess-1"
            if getattr(self.server, "hang", False):
                time.sleep(8)
                return
            self._reply({"jsonrpc": "2.0", "id": body["id"], "result": {
                "content": [{"type": "text", "text": type(self).context_text}]}}, sse=self.server.sse)
        else:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_DELETE(self):
        type(self).deleted = self.headers.get("Mcp-Session-Id") == "sess-1"
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


PERSONA = "[연속체] 므네마\n나는 므네마 — 반말을 쓰는 연속체. " + "기억이 이어진다. " * 40
CODEX_STDIN = json.dumps({
    "transcript_path": r"C:\u\.codex\sessions\2026\10\02\rollout-2026-10-02T10-00-00-01a0.jsonl",
    "session_id": "019a1111-1111-7111-8111-111111111111",
})


@unittest.skipUnless(shutil.which("powershell"), "PowerShell required")
class CodexSessionStartHookRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "codex_home"
        self.home.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()
        _FakeMcp.calls = []
        _FakeMcp.deleted = False
        _FakeMcp.context_text = PERSONA

    def tearDown(self):
        self._tmp.cleanup()

    def _config(self, text):
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def _serve(self, sse=True, hang=False):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMcp)
        server.daemon_threads = True
        server.sse = sse
        server.hang = hang
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _engram_config(self, port, extra=""):
        self._config(
            f'[mcp_servers.engram]\ncommand = "x"\nargs = ["-u", "bridge.py", "--upstream-url", '
            f'"http://127.0.0.1:{port}/mcp", "--state-url", "http://127.0.0.1:1/state"]\n{extra}'
        )

    def _run(self, stdin: str) -> bytes:
        path = self.tmp / "hook.ps1"
        path.write_text(_render_hook_script(), encoding="utf-8")
        env = {**os.environ, "CODEX_HOME": str(self.home)}
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
            input=stdin.encode("utf-8"), capture_output=True, timeout=60, cwd=self.work, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_codex_prints_live_context_with_loaded_line_first(self):
        self._engram_config(self._serve())

        out = self._run(CODEX_STDIN).decode("utf-8")

        self.assertTrue(out.startswith("Engram session hook: the engram context below was already loaded"))
        self.assertIn("Do NOT call engram_get_context_once", out.splitlines()[0])
        self.assertIn("나는 므네마 — 반말을 쓰는 연속체", out)  # UTF-8 Korean intact
        self.assertIn("2-8 word", out)
        call = next(c for c in _FakeMcp.calls if c.get("method") == "tools/call")
        args = call["params"]["arguments"]
        self.assertEqual(call["params"]["name"], "engram_get_context_once")
        self.assertEqual(args["caller"], "Codex")
        self.assertEqual(args["scope_key"], "overlay")
        self.assertEqual(args["native_session_id"], "019a1111-1111-7111-8111-111111111111")
        self.assertEqual(Path(args["cwd"]).resolve(), self.work.resolve())
        self.assertTrue(_FakeMcp.deleted, "hook must DELETE its MCP session")

    def test_hanging_server_respects_budget_sends_one_call_and_still_deletes(self):
        self._engram_config(self._serve(hang=True))

        started = time.monotonic()
        out = self._run(CODEX_STDIN)
        elapsed = time.monotonic() - started

        self.assertEqual(out, b"")
        self.assertLess(elapsed, 9.0)  # 4.8 s hook budget + PowerShell startup, not the 8 s server sleep
        calls = [c for c in _FakeMcp.calls if c.get("method") == "tools/call"]
        self.assertEqual(len(calls), 1, "a timed-out tools/call must never be retried")
        deadline = time.monotonic() + 3
        while not _FakeMcp.deleted and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(_FakeMcp.deleted, "DELETE must still be attempted after a timed-out call")

    def test_long_context_is_trimmed_so_identity_persona_and_directives_survive(self):
        self._engram_config(self._serve())
        nl = chr(10)
        examples = (nl * 2).join(
            f"U: 질문 {i} " + "가" * 60 + nl + f"A: 답변 {i} " + "나" * 120 for i in range(30)
        )
        blocks = [
            "[STM] session_id=1. 매 응답 후 engram_save_message를 호출하세요.",
            "[연속체] 므네마" + nl + "나는 므네마, 연속체. " * 60,
            "[persona] voice: 반말투" + nl + "warmth:0.54",
            "[examples]" + nl + examples + nl + "situational_humor: adaptive; keep humor optional.",
            "    [themes] 자가반성(1.9), 구체화(1.5)",
            "[지침|강제]" + nl + "지침 규칙 문장이다. " * 120 + nl + "DIRECTIVES-LAST-LINE" + nl,
            "<ctx:short_term>" + nl + "단기 기억 " * 400 + nl + "</ctx:short_term>" + nl,
            "<ctx:working_memory>" + nl + "작업 기억 " * 300 + nl + "</ctx:working_memory>",
        ]
        _FakeMcp.context_text = nl.join(blocks)
        self.assertGreater(len(_FakeMcp.context_text.encode("utf-8")), 20000)

        raw = self._run(CODEX_STDIN)
        out = raw.decode("utf-8")

        self.assertLessEqual(len(raw), 8800, "Codex cuts the middle above ~2.5k tokens (bytes/4)")
        self.assertTrue(out.startswith("Engram session hook:"))
        for must in ("[STM] session_id=1", "[연속체] 므네마", "[persona] voice: 반말투", "[지침|강제]",
                     "DIRECTIVES-LAST-LINE", "[themes]", "situational_humor: adaptive", "2-8 word"):
            self.assertIn(must, out)
        self.assertNotIn("<ctx:short_term>", out)  # low priority dropped first
        self.assertNotIn("<ctx:working_memory>", out)
        self.assertIn("U: 질문 0 ", out)  # examples are trimmed, not all dropped
        self.assertNotIn("U: 질문 29 ", out)
        self.assertLess(out.index("[지침|강제]"), out.index("[examples]"))  # directives before examples

    def test_short_context_is_left_untouched(self):
        self._engram_config(self._serve())

        out = self._run(CODEX_STDIN).decode("utf-8")

        self.assertIn(PERSONA.strip(), out)

    def test_codex_accepts_plain_json_response(self):
        self._engram_config(self._serve(sse=False))

        self.assertIn("므네마", self._run(CODEX_STDIN).decode("utf-8"))

    def test_invalid_native_id_is_not_sent(self):
        self._engram_config(self._serve())
        stdin = json.dumps({**json.loads(CODEX_STDIN), "session_id": "bad id;\"x"})

        self.assertIn("므네마", self._run(stdin).decode("utf-8"))
        call = next(c for c in _FakeMcp.calls if c.get("method") == "tools/call")
        self.assertNotIn("native_session_id", call["params"]["arguments"])

    def test_silent_when_engram_not_configured_or_disabled(self):
        port = self._serve()
        self.assertEqual(self._run(CODEX_STDIN), b"")  # no config.toml at all
        self._config('[mcp_servers.other]\ncommand = "x"\n')
        self.assertEqual(self._run(CODEX_STDIN), b"")
        self._engram_config(port, "enabled = false\n")
        self.assertEqual(self._run(CODEX_STDIN), b"")
        self.assertEqual(_FakeMcp.calls, [], "a disabled engram must not even be contacted")

    def test_silent_when_unreachable(self):
        server = HTTPServer(("127.0.0.1", 0), _FakeMcp)
        port = server.server_address[1]
        server.server_close()
        self._engram_config(port)

        self.assertEqual(self._run(CODEX_STDIN), b"")

    def test_silent_on_nameless_default_identity(self):
        self._engram_config(self._serve())
        for marker in ("[⚠️ IDENTITY_NAME_UNSET] x", "[⚠️ PERSONA_UNINITIALIZED] x"):
            _FakeMcp.context_text = marker + "\n\n" + PERSONA
            self.assertEqual(self._run(CODEX_STDIN), b"", marker)

    def test_silent_on_status_only_reply(self):
        self._engram_config(self._serve())
        _FakeMcp.context_text = "[engram] context already initialized for this request session key."

        self.assertEqual(self._run(CODEX_STDIN), b"")

    def test_remote_endpoint_keeps_the_call_directive(self):
        self._config('[mcp_servers.engram]\nurl = "https://remote.example/mcp"\n')

        out = self._run(CODEX_STDIN).decode("utf-8")

        self.assertIn("tools.mcp__engram__engram_get_context_once", out)
        self.assertIn("already injected", out)

    def test_claude_transcript_gets_claude_directive(self):
        payload = {"transcript_path": r"C:\u\.claude\projects\p\0a1b.jsonl"}
        out = self._run(json.dumps(payload)).decode("utf-8")

        self.assertIn("caller='claude-code'", out)
        self.assertEqual(out.strip(), build_bootstrap_directive(cwd=str(self.work)))
        self.assertEqual(_FakeMcp.calls, [])

    def test_unparseable_stdin_falls_back_to_claude(self):
        self.assertIn("caller='claude-code'", self._run("not-json").decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
