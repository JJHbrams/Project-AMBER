import functools
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

try:
    import streamlit  # noqa: F401
except ModuleNotFoundError:
    class _CacheDecorator:
        def __call__(self, func=None, **_kwargs):
            def decorate(target):
                cached = functools.lru_cache(maxsize=None)(target)
                cached.clear = cached.cache_clear
                return cached

            return decorate(func) if func is not None else decorate

        def clear(self):
            return None

    streamlit_stub = types.ModuleType("streamlit")
    streamlit_stub.cache_resource = _CacheDecorator()
    streamlit_stub.cache_data = _CacheDecorator()
    sys.modules["streamlit"] = streamlit_stub

from core.dashboard import data_access
from overlay.main import OverlayApp


class DashboardDbPathTests(unittest.TestCase):
    def tearDown(self):
        data_access.get_db.clear()

    def test_runtime_db_path_is_used_and_is_part_of_connection_cache_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_path = root / "first" / "engram.db"
            second_path = root / "second" / "engram.db"
            first_path.parent.mkdir()
            second_path.parent.mkdir()

            first = sqlite3.connect(first_path)
            second = sqlite3.connect(second_path)
            try:
                first.execute("CREATE TABLE marker (value TEXT)")
                first.execute("INSERT INTO marker VALUES ('first')")
                first.commit()
                second.execute("CREATE TABLE marker (value TEXT)")
                second.execute("INSERT INTO marker VALUES ('second')")
                second.commit()
            finally:
                first.close()
                second.close()

            first_cached = data_access.get_db(str(first_path))
            second_cached = data_access.get_db(str(second_path))
            try:
                self.assertIsNot(first_cached, second_cached)
                self.assertEqual(first_cached.execute("SELECT value FROM marker").fetchone()[0], "first")
                self.assertEqual(second_cached.execute("SELECT value FROM marker").fetchone()[0], "second")
                with patch.object(data_access, "get_db_root_dir", return_value=str(second_path.parent)):
                    self.assertEqual(data_access.get_db_path(), second_path)
                    self.assertEqual(data_access.query("SELECT value FROM marker"), [{"value": "second"}])
            finally:
                first_cached.close()
                second_cached.close()

    def test_overlay_passes_selected_db_directory_to_dashboard_process(self):
        app = OverlayApp.__new__(OverlayApp)
        process = unittest.mock.Mock(pid=4321)

        with patch.object(sys, "frozen", False, create=True), patch(
            "overlay.main.load_cfg", return_value={"dashboard": {"port": 8501}}
        ), patch("overlay.main._find_mcp_python", return_value=sys.executable), patch(
            "core.config.runtime_config.get_db_root_dir", return_value="E:/existing-wiki"
        ), patch("subprocess.Popen", return_value=process) as popen, patch(
            "builtins.open", mock_open()
        ):
            result = app._start_dashboard()

        self.assertIs(result, process)
        self.assertEqual(popen.call_args.kwargs["env"]["ENGRAM_DB_DIR"], "E:/existing-wiki")


if __name__ == "__main__":
    unittest.main()
