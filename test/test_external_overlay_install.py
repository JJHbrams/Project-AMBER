"""Ownership and eligibility rules for installing the external renderer.

These count causes, not symptoms. The renderer runtime is a single shared
directory (`%LOCALAPPDATA%\\engram-overlay\\runtime`) and autostart is a single
registry value, so an installer that assumes it owns them will delete work the
user did with the SDK. See docs/dev/external-overlay-install-plan.md §7.1-7.4.

The PowerShell is exercised in an isolated LOCALAPPDATA so the developer's own
runtime is never touched; tests skip when PowerShell is unavailable.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "installer" / "external-overlay.ps1"
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def run_ps(body: str, sandbox: Path) -> str:
    """Run a snippet with the script dot-sourced and LOCALAPPDATA redirected."""
    prelude = (
        f"$env:LOCALAPPDATA = '{sandbox}'; "
        f". '{SCRIPT}'; "
    )
    # 다른 테스트가 subprocess.run 을 전역으로 패치하면(patch.object(mod.subprocess,
    # "run") 는 전역 모듈을 건드린다) 여기서 조용히 이상한 값을 받는다. 진짜 프로세스를
    # 돌리는 테스트이므로 그 사실을 먼저 확인한다.
    runner = getattr(subprocess.run, "__module__", "")
    if runner != "subprocess":
        raise AssertionError(
            f"subprocess.run is patched by another test ({subprocess.run!r}); "
            "this test needs the real one"
        )
    completed = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", prelude + body],
        capture_output=True, text=True, timeout=600,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "powershell failed "
            f"(rc={completed.returncode!r}, stderr={completed.stderr!r}, "
            f"stdout={completed.stdout!r})"
        )
    out = (completed.stdout or "").strip()
    if not out:
        # rc 는 0 인데 출력이 없으면 스크립트가 조용히 죽은 것이다. 무엇이 보였는지
        # 말해야 원인을 찾을 수 있다 — 빈 문자열을 파싱하다 터지면 아무것도 모른다.
        raise AssertionError(
            f"powershell produced no output (rc={completed.returncode}, "
            f"stderr={completed.stderr!r}, body={body!r})"
        )
    return out


@unittest.skipIf(PWSH is None or os.name != "nt", "requires Windows PowerShell")
class ExternalOverlayOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="amber-overlay-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.runtime = self.sandbox / "engram-overlay" / "runtime"

    def test_untouched_runtime_without_a_marker_is_not_ours(self):
        # Stand in for a runtime the user created with install-runtime.ps1.
        self.runtime.mkdir(parents=True)
        (self.runtime / "Scripts").mkdir()
        out = run_ps(
            "$o = Get-ExternalOverlayOwnership; "
            "\"$($o.Exists)|$($o.OwnedByAmber)\"",
            self.sandbox,
        )
        self.assertEqual(out, "True|False")

    def test_install_refuses_to_claim_a_runtime_it_did_not_create(self):
        self.runtime.mkdir(parents=True)
        marker_before = sorted(p.name for p in self.runtime.iterdir())
        out = run_ps(
            "$r = Install-ExternalOverlayRuntime -WheelPath 'C:\\nonexistent.whl' "
            "-AmberVersion '9.9.9.9'; \"$($r.Installed)|$($r.Skipped)\"",
            self.sandbox,
        )
        installed, skipped = out.split("|")
        self.assertEqual(installed, "False")
        self.assertEqual(skipped, "True", "an existing unmarked runtime must be left alone")
        self.assertFalse((self.runtime / ".installed-by-amber").exists(),
                         "claiming ownership by writing a marker is exactly what must not happen")
        self.assertEqual(sorted(p.name for p in self.runtime.iterdir()), marker_before)

    def test_remove_refuses_without_a_marker(self):
        self.runtime.mkdir(parents=True)
        (self.runtime / "keep.txt").write_text("user work", encoding="utf-8")
        out = run_ps(
            "$d = Remove-ExternalOverlayRuntime; \"$($d.Removed)\"",
            self.sandbox,
        )
        self.assertEqual(out, "False")
        self.assertTrue((self.runtime / "keep.txt").is_file(),
                        "the user's own runtime survived")

    def test_remove_deletes_only_a_marked_runtime(self):
        self.runtime.mkdir(parents=True)
        (self.runtime / ".installed-by-amber").write_text(
            json.dumps({"amber_version": "9.9.9.9", "interpreter": "C:\\python.exe",
                        "autostart_value": ""}),
            encoding="utf-8",
        )
        out = run_ps("$d = Remove-ExternalOverlayRuntime; \"$($d.Removed)\"", self.sandbox)
        self.assertEqual(out, "True")
        self.assertFalse(self.runtime.exists())

    def test_health_reports_a_vanished_base_interpreter(self):
        # A Windows venv is not self-contained: pyvenv.cfg points at the base
        # install. If that goes away the renderer stops starting, silently.
        self.runtime.mkdir(parents=True)
        (self.runtime / "Scripts").mkdir()
        (self.runtime / "Scripts" / "engram-custom-overlayw.exe").write_bytes(b"")
        (self.runtime / ".installed-by-amber").write_text(
            json.dumps({"amber_version": "9.9.9.9",
                        "interpreter": str(self.sandbox / "gone" / "python.exe"),
                        "autostart_value": ""}),
            encoding="utf-8",
        )
        out = run_ps(
            "$h = Test-ExternalOverlayRuntimeHealth; \"$($h.Healthy)|$($h.Reason)\"",
            self.sandbox,
        )
        healthy, reason = out.split("|", 1)
        self.assertEqual(healthy, "False")
        self.assertIn("Python", reason)


@unittest.skipIf(PWSH is None or os.name != "nt", "requires Windows PowerShell")
class ExternalOverlayEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Path(tempfile.mkdtemp(prefix="amber-overlay-test-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)

    def test_missing_interpreter_is_reported_not_thrown(self):
        out = run_ps(
            "$p = Resolve-EligiblePython -Command 'definitely-not-python'; "
            "\"$($p.Ok)|$([bool]$p.Reason)\"",
            self.sandbox,
        )
        self.assertEqual(out, "False|True", "an unusable Python must give a reason, not an exception")

    def test_real_interpreter_is_accepted_with_a_version(self):
        out = run_ps(
            "$p = Resolve-EligiblePython; \"$($p.Ok)|$($p.Version)\"", self.sandbox
        )
        ok, version = out.split("|")
        if ok != "True":
            self.skipTest("no eligible system Python on this machine")
        major, minor = (int(part) for part in version.split("."))
        self.assertGreaterEqual((major, minor), (3, 11),
                                "eligibility must enforce the renderer's requires-python")

    def test_pin_is_read_from_the_installer_directory(self):
        out = run_ps("Get-PinnedExternalOverlayVersion", self.sandbox)
        expected = (ROOT / "installer" / "external-overlay.pin").read_text(encoding="utf-8").strip()
        self.assertEqual(out, expected)


if __name__ == "__main__":
    unittest.main()
