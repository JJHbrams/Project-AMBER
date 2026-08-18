import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.install import process_identity


ROOT = Path(__file__).resolve().parents[1]


def _identity(executable: Path, arguments: str, *, pid: int = 2828, parent: int = 45540) -> dict:
    return {
        "ProcessId": pid,
        "ParentProcessId": parent,
        "Name": executable.name,
        "ExecutablePath": str(executable),
        "CommandLine": f'"{executable}" {arguments}'.strip(),
    }


class InstalledFrozenChildAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.local_app_data = Path(r"C:\Users\tester\AppData\Local")
        self.bundle = self.local_app_data / "Programs" / "EngramOverlay" / "dist" / "engram-overlay"
        self.environment = {"LOCALAPPDATA": str(self.local_app_data)}

    def test_accepts_only_mcp_and_watcher_roles_from_default_installed_overlay(self):
        executable = self.bundle / "engram-overlay.exe"
        with patch.dict(os.environ, self.environment, clear=False):
            self.assertTrue(process_identity.is_default_installed_frozen_child(
                _identity(executable, "--role mcp-server --port 17385")
            ))
            self.assertTrue(process_identity.is_default_installed_frozen_child(
                _identity(executable, "--role kg-watcher")
            ))
            self.assertFalse(process_identity.is_default_installed_frozen_child(
                _identity(executable, "--role overlay")
            ))

    def test_accepts_default_installed_dashboard_without_backend_role(self):
        executable = self.bundle / "engram-dashboard.exe"
        with patch.dict(os.environ, self.environment, clear=False):
            self.assertTrue(process_identity.is_default_installed_frozen_child(
                _identity(executable, "--port 8501")
            ))
            self.assertFalse(process_identity.is_default_installed_frozen_child(
                _identity(executable, "--role mcp-server")
            ))

    def test_rejects_identical_layout_outside_trusted_install_root(self):
        executable = Path(r"C:\untrusted\EngramOverlay\dist\engram-overlay\engram-overlay.exe")
        with patch.dict(os.environ, self.environment, clear=False):
            self.assertFalse(process_identity.is_default_installed_frozen_child(
                _identity(executable, "--role kg-watcher")
            ))

    def test_dev_restart_cleanup_includes_frozen_mcp_watcher_and_dashboard_only(self):
        overlay = self.bundle / "engram-overlay.exe"
        dashboard = self.bundle / "engram-dashboard.exe"
        candidates = [
            _identity(overlay, "--role mcp-server", pid=2828),
            _identity(overlay, "--role kg-watcher", pid=41316),
            _identity(dashboard, "--port 8501", pid=51515),
            _identity(overlay, "--role policy-preflight", pid=61616),
        ]
        with patch.dict(os.environ, self.environment, clear=False), patch.object(
            process_identity, "list_candidate_processes", return_value=candidates
        ), patch.object(process_identity, "terminate_identity_exact", return_value=True) as terminate:
            stopped = process_identity.cleanup_dev_restart_orphans(ROOT)
        self.assertEqual(stopped, [2828, 41316, 51515])
        self.assertEqual(terminate.call_count, 3)


class SourceChildIdentityTests(unittest.TestCase):
    def test_accepts_same_checkout_mcp_watcher_and_dashboard_commands(self):
        python = Path(r"C:\Miniconda\envs\intel_engram\python.exe")
        streamlit = python.parent / "streamlit.exe"
        identities = (
            _identity(python, f'"{ROOT / "mcp_server.py"}" --port 17385'),
            _identity(python, f'"{ROOT / "scripts" / "kg" / "kg_watcher.py"}"'),
            _identity(streamlit, f'run "{ROOT / "scripts" / "engram_dashboard.py"}" --server.port 8501'),
        )
        for identity in identities:
            self.assertTrue(process_identity.is_same_checkout_source_child(identity, ROOT))
            self.assertFalse(process_identity.is_same_checkout_source_child(identity, ROOT / "other"))

        arbitrary = _identity(Path(r"C:\Tools\other-server.exe"), f'"{ROOT / "mcp_server.py"}"')
        self.assertFalse(process_identity.is_same_checkout_source_child(arbitrary, ROOT))

    def test_snapshot_requires_exact_parent_and_checkout(self):
        child = _identity(
            Path(r"C:\Miniconda\python.exe"),
            f'"{ROOT / "mcp_server.py"}" --port 17385',
            parent=45540,
        )
        wrong_parent = dict(child, ProcessId=38425, ParentProcessId=999)
        unrelated = _identity(Path(r"C:\Tools\server.exe"), "--port 17385", pid=3131, parent=45540)
        with patch.object(process_identity, "list_candidate_processes", return_value=[child, wrong_parent, unrelated]):
            snapshot = process_identity.snapshot_source_children(45540, ROOT)
        self.assertEqual([item["ProcessId"] for item in snapshot], [child["ProcessId"]])

    def test_cleanup_snapshot_rechecks_identity_and_allows_reparenting_only(self):
        original = _identity(
            Path(r"C:\Miniconda\python.exe"),
            f'"{ROOT / "mcp_server.py"}" --port 17385',
            pid=38424,
            parent=45540,
        )
        reparented = dict(original, ParentProcessId=1)
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.TerminateProcess.return_value = 1
        with patch.object(process_identity, "get_process_identity", return_value=reparented), patch.object(
            process_identity.ctypes, "windll", Mock(kernel32=kernel), create=True
        ):
            stopped = process_identity.cleanup_source_snapshot([original], ROOT)
        self.assertEqual(stopped, [38424])
        kernel.TerminateProcess.assert_called_once_with(123, 0)

    def test_cleanup_snapshot_rejects_pid_reuse_or_command_change(self):
        original = _identity(
            Path(r"C:\Miniconda\python.exe"),
            f'"{ROOT / "mcp_server.py"}" --port 17385',
            pid=38424,
        )
        changed = dict(original, CommandLine=str(original["CommandLine"]) + " --different")
        kernel = Mock()
        with patch.object(process_identity, "get_process_identity", return_value=changed), patch.object(
            process_identity.ctypes, "windll", Mock(kernel32=kernel), create=True
        ):
            stopped = process_identity.cleanup_source_snapshot([original], ROOT)
        self.assertEqual(stopped, [])
        kernel.OpenProcess.assert_not_called()

    def test_reparented_cleanup_considers_only_exact_checkout_commands(self):
        source_child = _identity(
            Path(r"C:\Miniconda\python.exe"),
            f'"{ROOT / "scripts" / "kg" / "kg_watcher.py"}"',
            pid=41317,
            parent=1,
        )
        other_checkout = dict(
            source_child,
            ProcessId=41318,
            CommandLine=str(source_child["CommandLine"]).replace(str(ROOT), str(ROOT / "other")),
        )
        with patch.object(
            process_identity, "list_candidate_processes", return_value=[source_child, other_checkout]
        ), patch.object(process_identity, "terminate_identity_exact", return_value=True) as terminate:
            stopped = process_identity.cleanup_same_checkout_source_orphans(ROOT)
        self.assertEqual(stopped, [41317])
        self.assertEqual(terminate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
