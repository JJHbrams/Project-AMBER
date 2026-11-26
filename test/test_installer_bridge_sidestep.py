"""Setup must not be blocked by a client session's running engram-mcp-bridge.exe."""
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "installer" / "sidestep-engram-bridge.ps1"
STOPPER = ROOT / "installer" / "stop-engram-processes.ps1"
ISS = ROOT / "installer" / "engram-overlay.iss"
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

OLD_NAME = re.compile(r"^engram-mcp-bridge\.exe\.\d{14}\.old$")


@unittest.skipUnless(os.name == "nt" and POWERSHELL.exists(), "Windows PowerShell 5.1 required")
class BridgeSidestepHelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        self.root = Path(self._tmp.name)
        self.artifact = self.root / "dist" / "engram-overlay"
        self.artifact.mkdir(parents=True)
        self.manifest = self.root / "state" / "bridge-aside.txt"
        self.procs = []

    def _cleanup(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        for _ in range(20):
            try:
                self._tmp.cleanup()
                return
            except PermissionError:
                time.sleep(0.3)

    def _bridge_exe(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        exe = directory / "engram-mcp-bridge.exe"
        shutil.copyfile(POWERSHELL, exe)
        return exe

    def _run_exe(self, exe):
        proc = subprocess.Popen(
            [str(exe), "-NoProfile", "-Command", "Start-Sleep -Seconds 120"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        time.sleep(1.5)
        self.assertIsNone(proc.poll(), "copied exe should be running")
        return proc

    def _helper(self, action):
        args = [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HELPER), "-Action", action]
        if action != "Restore":
            args += ["-ArtifactDir", str(self.artifact)]
        if action != "Cleanup":
            args += ["-ManifestPath", str(self.manifest)]
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _old_files(self):
        return [p for p in self.artifact.iterdir() if OLD_NAME.match(p.name)]

    def test_running_bridge_is_renamed_and_keeps_running(self):
        exe = self._bridge_exe(self.artifact)
        proc = self._run_exe(exe)

        self._helper("Sidestep")

        self.assertFalse(exe.exists())
        self.assertEqual(len(self._old_files()), 1)
        self.assertIsNone(proc.poll())
        # The slot is free: a new bridge copy can land there while the session runs on.
        exe.write_bytes(b"new bridge")
        self.assertTrue(exe.exists())

    def test_restore_puts_the_original_name_back(self):
        exe = self._bridge_exe(self.artifact)
        proc = self._run_exe(exe)
        self._helper("Sidestep")

        self._helper("Restore")

        self.assertTrue(exe.exists())
        self.assertEqual(self._old_files(), [])
        self.assertFalse(self.manifest.exists())
        self.assertIsNone(proc.poll())

    def test_restore_keeps_aside_file_when_original_exists(self):
        exe = self._bridge_exe(self.artifact)
        self._run_exe(exe)
        self._helper("Sidestep")
        exe.write_bytes(b"newly installed")

        result = self._helper("Restore")

        self.assertEqual(exe.read_bytes(), b"newly installed")
        self.assertEqual(len(self._old_files()), 1)
        self.assertIn("original already exists", result.stdout)

    def test_bridge_not_running_is_left_alone(self):
        exe = self._bridge_exe(self.artifact)

        self._helper("Sidestep")

        self.assertTrue(exe.exists())
        self.assertEqual(self._old_files(), [])
        self.assertFalse(self.manifest.exists())

    def test_same_name_process_from_another_path_is_ignored(self):
        installed = self._bridge_exe(self.artifact)
        elsewhere = self._bridge_exe(self.root / "elsewhere")
        self._run_exe(elsewhere)

        self._helper("Sidestep")

        self.assertTrue(installed.exists())
        self.assertEqual(self._old_files(), [])
        self.assertTrue(elsewhere.exists())

    def test_missing_artifact_bridge_is_a_no_op(self):
        self._helper("Sidestep")
        self.assertEqual(list(self.artifact.iterdir()), [])

    def test_cleanup_removes_only_unlocked_old_files(self):
        exe = self._bridge_exe(self.artifact)
        self._run_exe(exe)
        self._helper("Sidestep")
        (locked,) = self._old_files()
        stale = self.artifact / "engram-mcp-bridge.exe.20200101000000.old"
        stale.write_bytes(b"old")
        unrelated = self.artifact / "engram-mcp-bridge.exe.keep.old"
        unrelated.write_bytes(b"keep")

        self._helper("Cleanup")

        self.assertFalse(stale.exists())
        self.assertTrue(locked.exists(), "a file a session still runs from must survive")
        self.assertTrue(unrelated.exists())

    def test_helper_is_ascii_with_crlf_for_windows_powershell(self):
        raw = HELPER.read_bytes()
        raw.decode("ascii")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_stop_helper_can_record_stopped_overlays(self):
        text = STOPPER.read_text(encoding="utf-8-sig")
        self.assertIn("$StoppedOverlayListPath", text)
        self.assertIn("$result.OverlayPaths", text)


BS = chr(92)
CRLF = chr(10)  # read_text() normalises line ends


class InstallerScriptWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iss = ISS.read_text(encoding="utf-8-sig")

    def _body(self, header):
        start = self.iss.index(header)
        end = self.iss.find(CRLF + "end;", start)
        return self.iss[start:end]

    def test_iss_keeps_bom_and_crlf(self):
        raw = ISS.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_helper_is_extracted_and_shipped(self):
        self.assertIn('Source: "sidestep-engram-bridge.ps1"; Flags: dontcopy', self.iss)
        shipped = 'Source: "sidestep-engram-bridge.ps1"; DestDir: "{app}' + BS + 'installer"'
        self.assertIn(shipped, self.iss)
        self.assertIn("ExtractTemporaryFile('sidestep-engram-bridge.ps1')", self.iss)

    def test_prepare_to_install_sidesteps_after_stopping_processes(self):
        body = self._body("function PrepareToInstall")
        stop = body.index("process-stop helper exit=")
        sidestep = body.index("RunBridgeHelper('PrepareToInstall', 'Sidestep')")
        self.assertLess(stop, sidestep)
        # Other locked files must keep failing: Restart Manager settings stay as they were.
        self.assertIn("CloseApplications=yes", self.iss)
        self.assertIn("CloseApplicationsFilter=*.exe,*.dll,*.pyd", self.iss)

    def test_ss_install_repeats_the_sidestep(self):
        self.assertIn("RunBridgeHelper('ssInstall', 'Sidestep')", self.iss)

    def test_success_flag_is_set_only_after_configure(self):
        step = self._body("procedure CurStepChanged")
        configure = step.index("RunConfigure;")
        flag = step.index("InstallCompleted := True;")
        cleanup = step.index("RunBridgeHelper('ssPostInstall', 'Cleanup')")
        self.assertLess(configure, flag)
        self.assertLess(flag, cleanup)
        self.assertEqual(self.iss.count("InstallCompleted := True;"), 1)

    def test_deinitialize_restores_when_install_did_not_complete(self):
        body = self._body("procedure DeinitializeSetup")
        guard = body.index("if not InstallCompleted then")
        self.assertLess(guard, body.index("RunBridgeHelper('DeinitializeSetup', 'Restore')"))
        self.assertLess(guard, body.index("RelaunchStoppedOverlays"))

    def test_overlay_relaunch_is_plain_visible_nowait(self):
        body = self._body("procedure RelaunchStoppedOverlays")
        self.assertIn("SW_SHOWNORMAL, ewNoWait", body)
        self.assertNotIn("SW_HIDE", body)

    def test_stop_helper_receives_overlay_list_path(self):
        self.assertIn("-StoppedOverlayListPath", self.iss)

    def test_uninstall_delete_covers_old_bridges(self):
        self.assertIn("[UninstallDelete]", self.iss)
        entry = 'Type: files; Name: "{app}' + BS + "dist" + BS + "engram-overlay" + BS + 'engram-mcp-bridge.exe.*.old"'
        self.assertIn(entry, self.iss)
