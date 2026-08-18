import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.kg import kg_watcher


class WatcherWindowsLivenessTests(unittest.TestCase):
    def _kernel(self, exit_code: int, query_success: bool = True) -> Mock:
        kernel = Mock()
        kernel.OpenProcess.return_value = 123

        def get_exit_code(handle, pointer):
            pointer._obj.value = exit_code
            return 1 if query_success else 0

        kernel.GetExitCodeProcess.side_effect = get_exit_code
        return kernel

    def test_still_active_process_is_alive_and_handle_is_closed(self):
        kernel = self._kernel(259)
        with patch.object(kg_watcher.sys, "platform", "win32"), patch.object(
            ctypes, "windll", Mock(kernel32=kernel), create=True
        ):
            self.assertTrue(kg_watcher._is_process_alive(41316))

        kernel.GetExitCodeProcess.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_terminated_process_object_is_not_alive_and_handle_is_closed(self):
        kernel = self._kernel(0)
        with patch.object(kg_watcher.sys, "platform", "win32"), patch.object(
            ctypes, "windll", Mock(kernel32=kernel), create=True
        ):
            self.assertFalse(kg_watcher._is_process_alive(41316))

        kernel.GetExitCodeProcess.assert_called_once()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_failed_exit_code_query_is_not_alive_and_handle_is_closed(self):
        kernel = self._kernel(259, query_success=False)
        with patch.object(kg_watcher.sys, "platform", "win32"), patch.object(
            ctypes, "windll", Mock(kernel32=kernel), create=True
        ):
            self.assertFalse(kg_watcher._is_process_alive(41316))

        kernel.CloseHandle.assert_called_once_with(123)


class WatcherSingletonLockTests(unittest.TestCase):
    def test_terminated_pid_lock_is_replaced_by_current_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "kg_watcher.lock"
            lock_path.write_text("41316", encoding="utf-8")
            with patch.object(kg_watcher, "_is_process_alive", return_value=False), patch.object(
                kg_watcher.os, "getpid", return_value=8136
            ), patch.object(kg_watcher.atexit, "register"):
                self.assertTrue(kg_watcher._acquire_singleton_lock(lock_path))

            self.assertEqual(lock_path.read_text(encoding="utf-8"), "8136")


if __name__ == "__main__":
    unittest.main()
