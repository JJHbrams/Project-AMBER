"""Install-in-progress marker, bounded execution and Codex wrapper routing (issue #14)."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.integrations import engram_bootstrap as eb

IS_WINDOWS = os.name == "nt"
ISS = ROOT / "installer" / "engram-overlay.iss"


class WrapperTextTests(unittest.TestCase):
    def test_marker_check_and_age_logic_are_rendered(self):
        for provider in ("claude-code", "codex", "gemini", "copilot"):
            script = eb._render_pretool_hook_script(provider)
            self.assertIn("'install-in-progress'", script)
            self.assertIn("$markerMaxAgeSeconds = 600", script)
            self.assertIn("-lt $markerMaxAgeSeconds", script)
            self.assertIn("exit 0", script)
            # marker check happens before the backend is launched
            self.assertLess(script.index("-lt $markerMaxAgeSeconds"), script.index("Process]::Start"))

    def test_timeout_and_tree_kill_and_stdin_forwarding(self):
        script = eb._render_pretool_hook_script()
        self.assertIn("$backendTimeoutMs = 20000", script)
        self.assertIn("$clock.ElapsedMilliseconds -ge $backendTimeoutMs", script)
        self.assertIn("Stop-Descendants", script)
        self.assertIn("taskkill.exe /T /F /PID", script)
        self.assertIn("[Console]::In.ReadToEnd()", script)
        self.assertIn("StandardInput.BaseStream.WriteAsync", script)

    def test_ps1_is_written_with_bom_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.ps1"
            content = eb._render_pretool_hook_script("codex")
            self.assertFalse(eb._script_current(path, content))
            path.write_bytes(eb._script_bytes(path, content))
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertTrue(eb._script_current(path, content))
            sh = Path(tmp) / "w.sh"
            self.assertFalse(eb._script_bytes(sh, "#!/bin/sh\n").startswith(b"\xef\xbb\xbf"))

    def test_neutral_output_only_for_antigravity_dialect(self):
        self.assertIn("$neutralOutput = '{\"decision\":\"allow\"}'", eb._render_pretool_hook_script("antigravity"))
        for provider in ("claude-code", "codex", "copilot"):
            self.assertIn("$neutralOutput = ''", eb._render_pretool_hook_script(provider))

    def test_exit_code_passthrough_only_for_codex(self):
        self.assertIn("$passExitCode = $true", eb._render_pretool_hook_script("codex"))
        self.assertIn("$passExitCode = $false", eb._render_pretool_hook_script("claude-code"))

    def test_marker_name_matches_installer(self):
        iss = ISS.read_text(encoding="utf-8-sig")
        self.assertIn(eb.INSTALL_IN_PROGRESS_MARKER_NAME, iss)
        self.assertEqual(eb.INSTALL_MARKER_MAX_AGE_SECONDS, 600)
        self.assertEqual(eb.HOOK_BACKEND_TIMEOUT_SECONDS, 20)


@unittest.skipUnless(IS_WINDOWS, "Windows PowerShell wrapper")
class WrapperFunctionalTests(unittest.TestCase):
    def _render(self, base: Path, child_source: str, provider: str = "claude-code") -> Path:
        child = base / "child.py"
        child.write_text(child_source, encoding="utf-8")
        parts = (sys.executable, [str(child), "--provider", provider])
        with patch.object(eb, "_agent_policy_hook_command_parts", return_value=parts):
            script = eb._render_pretool_hook_script(provider)
        path = base / "wrapper.ps1"
        path.write_bytes(eb._script_bytes(path, script))
        return path

    def _run(self, base: Path, script: Path, stdin: str = "{}", timeout: int = 90):
        env = os.environ.copy()
        env["USERPROFILE"] = str(base)
        env["HOME"] = str(base)
        started = time.monotonic()
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=base, timeout=timeout, check=False,
        )
        return done, time.monotonic() - started

    @staticmethod
    def _alive(pid: int) -> bool:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return re.search(rf"\b{pid}\b", out) is not None

    def test_fresh_marker_skips_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / ".engram").mkdir()
            (base / ".engram" / "install-in-progress").write_text("x")
            ran = base / "ran.txt"
            script = self._render(base, f"open({str(ran)!r}, 'w').write('ran')\n")
            done, elapsed = self._run(base, script)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(done.stdout, "")
            self.assertFalse(ran.exists())
            self.assertLess(elapsed, 10)

    def test_stale_marker_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / ".engram").mkdir()
            marker = base / ".engram" / "install-in-progress"
            marker.write_text("x")
            old = time.time() - 11 * 60
            os.utime(marker, (old, old))
            script = self._render(base, "print('ran-ok')\n")
            done, _ = self._run(base, script)
            self.assertEqual(done.returncode, 0)
            self.assertIn("ran-ok", done.stdout)

    def test_normal_child_output_stdin_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src = (
                "import sys\n"
                "data = sys.stdin.buffer.read().decode('utf-8')\n"
                "sys.stdout.buffer.write(('IN:' + data).encode('utf-8'))\n"
                "sys.stderr.write('warn')\n"
                "sys.exit(3)\n"
            )
            claude = self._render(base, src, "claude-code")
            done, _ = self._run(base, claude, stdin='{"k":"한글"}')
            self.assertEqual(done.returncode, 0)  # claude wrapper is fail-open
            self.assertIn('IN:{"k":"한글"}', done.stdout)
            self.assertIn("warn", done.stderr)
            codex = self._render(base, src, "codex")
            done, _ = self._run(base, codex, stdin="{}")
            self.assertEqual(done.returncode, 3)  # codex passes the exit code through
            self.assertIn("IN:{}", done.stdout)

    def test_non_ascii_exe_path_survives_powershell_51(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            odd = base / "홍길동 Programs"
            odd.mkdir()
            exe_dir = odd / "engram-overlay"
            exe_dir.mkdir()
            child = exe_dir / "child.py"
            child.write_text("print('odd-ok')\n", encoding="utf-8")
            parts = (sys.executable, [str(child), "--provider", "claude-code"])
            with patch.object(eb, "_agent_policy_hook_command_parts", return_value=parts):
                script = eb._render_pretool_hook_script("claude-code")
            wrapper = base / "wrapper.ps1"
            wrapper.write_bytes(eb._script_bytes(wrapper, script))
            done, _ = self._run(base, wrapper)
            self.assertEqual(done.returncode, 0)
            self.assertIn("odd-ok", done.stdout)

    def test_antigravity_neutral_allow_on_marker_timeout_free_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / ".engram").mkdir()
            (base / ".engram" / "install-in-progress").write_text("x")
            script = self._render(base, "print('should-not-run')\n", "antigravity")
            done, _ = self._run(base, script)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(json.loads(done.stdout), {"decision": "allow"})
            # backend failure also emits the neutral allow
            (base / ".engram" / "install-in-progress").unlink()
            script = self._render(base, "import sys\nsys.exit(1)\n", "antigravity")
            done, _ = self._run(base, script)
            self.assertEqual(json.loads(done.stdout), {"decision": "allow"})

    def test_grandchild_holding_pipes_is_killed_and_output_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pids = base / "gc.txt"
            src = (
                "import subprocess, sys\n"
                "sub = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(pids)!r}, 'w').write(str(sub.pid))\n"
                "print('partial', flush=True)\n"
            )
            script = self._render(base, src, "codex")
            done, elapsed = self._run(base, script)
            self.assertIn("partial", done.stdout)
            self.assertEqual(done.returncode, 0)
            self.assertLess(elapsed, 15)
            gc_pid = int(pids.read_text())
            time.sleep(1)
            try:
                self.assertFalse(self._alive(gc_pid))
            finally:
                subprocess.run(["taskkill", "/F", "/PID", str(gc_pid)], capture_output=True)

    def test_timeout_kills_whole_process_tree_and_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pids = base / "pids.txt"
            src = (
                "import os, subprocess, sys, time\n"
                "sub = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{sub.pid}}')\n"
                "time.sleep(40)\n"
            )
            script = self._render(base, src)
            done, elapsed = self._run(base, script)
            self.assertEqual(done.returncode, 0)
            self.assertLess(elapsed, 30)
            self.assertGreaterEqual(elapsed, 19)
            parent_pid, child_pid = (int(p) for p in pids.read_text().split())
            time.sleep(1)
            try:
                self.assertFalse(self._alive(parent_pid))
                self.assertFalse(self._alive(child_pid))
            finally:
                for pid in (parent_pid, child_pid):
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


class CodexHookRoutingTests(unittest.TestCase):
    def test_windows_command_uses_wrapper_not_exe(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(eb, "_ENGRAM_DIR", Path(tmp) / ".engram"):
            handler = eb._codex_hook_handler()
        self.assertTrue(handler["commandWindows"].startswith("powershell -NoProfile -ExecutionPolicy Bypass -File"))
        self.assertIn("engram-codex-pretool-hook.ps1", handler["commandWindows"])
        self.assertNotIn("agent-policy-hook", handler["commandWindows"])
        self.assertIn("agent-policy-hook", handler["command"])  # POSIX command unchanged
        self.assertEqual(handler["timeout"], 30)

    def test_sync_is_idempotent_and_writes_and_removes_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hooks_path = base / ".codex" / "hooks.json"
            with patch.object(eb, "_CODEX_HOOKS_PATH", hooks_path), patch.object(eb, "_ENGRAM_DIR", base / ".engram"):
                first = eb.sync_codex_pretool_hook(True)
                snapshot = hooks_path.read_text(encoding="utf-8")
                second = eb.sync_codex_pretool_hook(True)
                wrapper = base / ".engram" / "engram-codex-pretool-hook.ps1"
                self.assertTrue(first["ok"] and first["changed"] and first["trust_required"])
                self.assertFalse(second["changed"])
                self.assertEqual(snapshot, hooks_path.read_text(encoding="utf-8"))
                if IS_WINDOWS:
                    self.assertIn("$passExitCode = $true", wrapper.read_text(encoding="utf-8"))
                handler = json.loads(snapshot)["hooks"]["PreToolUse"][0]["hooks"][0]
                self.assertIn(str(wrapper), handler["commandWindows"])
                # An old install (direct exe) is rewritten once, then stable.
                old = json.loads(snapshot)
                old["hooks"]["PreToolUse"][0]["hooks"][0]["commandWindows"] = "old-engram-overlay.exe --role agent-policy-hook"
                hooks_path.write_text(json.dumps(old), encoding="utf-8")
                migrated = eb.sync_codex_pretool_hook(True)
                self.assertTrue(migrated["changed"])
                self.assertFalse(eb.sync_codex_pretool_hook(True)["changed"])
                eb.sync_codex_pretool_hook(False)
                self.assertFalse(wrapper.exists())


class InstallerScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iss = ISS.read_text(encoding="utf-8-sig")

    @staticmethod
    def _body(text: str, header: str) -> str:
        start = text.index(header)
        end = re.search(r"^end;", text[start:], re.M).end()
        return text[start:start + end]

    def test_uses_inno_userprofile_constant(self):
        self.assertIn("{%USERPROFILE}", self.iss)
        self.assertNotIn("{userprofile}", self.iss.lower().replace("{%userprofile}", ""))

    def test_marker_created_before_stop_script_in_prepare(self):
        body = self._body(self.iss, "function PrepareToInstall")
        self.assertIn("CreateInstallMarker", body)
        self.assertLess(body.index("CreateInstallMarker"), body.index("Exec("))

    def test_marker_removed_after_configure_and_on_deinit(self):
        step = self._body(self.iss, "procedure CurStepChanged")
        self.assertIn("RunConfigure", step)
        self.assertIn("finally", step)
        self.assertLess(step.index("RunConfigure"), step.index("RemoveInstallMarker"))
        self.assertIn("RemoveInstallMarker", self._body(self.iss, "procedure DeinitializeSetup"))

    def test_stop_script_rerun_at_ssinstall(self):
        step = self._body(self.iss, "procedure CurStepChanged")
        self.assertIn("ssInstall", step)
        self.assertIn("StopEngramProcesses", step[step.index("ssInstall"):])

    def test_marker_path_matches_python_constant(self):
        self.assertIn(r"{%USERPROFILE}\.engram\install-in-progress", self.iss)


if __name__ == "__main__":
    unittest.main()
