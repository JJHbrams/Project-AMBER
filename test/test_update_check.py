import json
import tempfile
import tkinter as tk
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from core.update.github_release import (
    ReleaseAsset,
    UpdateCheckError,
    UpdateResult,
    check_update,
    compare_versions,
    normalize_tag,
    select_asset,
)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fetch_returning(payload: dict):
    def _fetch(_request):
        return _FakeResponse(payload)

    return _fetch


def _fetch_raising(exc: Exception):
    def _fetch(_request):
        raise exc

    return _fetch


class NormalizeTagTests(unittest.TestCase):
    def test_three_part_tag_with_v_prefix(self):
        self.assertEqual(normalize_tag("v1.5.20"), (1, 5, 20))

    def test_three_part_tag_without_prefix(self):
        self.assertEqual(normalize_tag("1.5.20"), (1, 5, 20))

    def test_four_part_local_version(self):
        self.assertEqual(normalize_tag("1.5.20.825"), (1, 5, 20, 825))

    def test_malformed_tag_raises_korean_reason(self):
        with self.assertRaises(UpdateCheckError) as ctx:
            normalize_tag("not-a-version")
        self.assertIn("해석하지 못했습니다", ctx.exception.reason)


class CompareVersionsTests(unittest.TestCase):
    def test_three_part_tag_compares_only_major_minor_patch(self):
        # Local build number (.825) must not make the local version look
        # newer than a tag with the same Major.Minor.Patch.
        self.assertEqual(compare_versions("1.5.20.825", "v1.5.20"), 0)

    def test_local_behind_three_part_tag(self):
        self.assertLess(compare_versions("1.5.19.999", "v1.5.20"), 0)

    def test_local_ahead_of_three_part_tag(self):
        self.assertGreater(compare_versions("1.5.21.1", "v1.5.20"), 0)

    def test_four_part_tag_compares_all_parts(self):
        self.assertEqual(compare_versions("1.5.20.825", "1.5.20.825"), 0)
        self.assertLess(compare_versions("1.5.20.100", "1.5.20.825"), 0)


class SelectAssetTests(unittest.TestCase):
    def test_picks_matching_asset(self):
        assets = [
            {"name": "README.txt", "browser_download_url": "x", "size": 1, "created_at": "2026-01-01T00:00:00Z"},
            {
                "name": "AMBER_1.5.20_x64-setup.exe",
                "browser_download_url": "https://example.com/setup.exe",
                "size": 12345,
                "created_at": "2026-01-02T00:00:00Z",
            },
        ]
        asset = select_asset(assets)
        self.assertEqual(asset, ReleaseAsset(
            name="AMBER_1.5.20_x64-setup.exe",
            download_url="https://example.com/setup.exe",
            size=12345,
            created_at="2026-01-02T00:00:00Z",
        ))

    def test_picks_most_recent_when_multiple_match(self):
        assets = [
            {
                "name": "AMBER_1.5.19_x64-setup.exe",
                "browser_download_url": "https://example.com/old.exe",
                "size": 1,
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "name": "AMBER_1.5.20_x64-setup.exe",
                "browser_download_url": "https://example.com/new.exe",
                "size": 2,
                "created_at": "2026-02-01T00:00:00Z",
            },
        ]
        asset = select_asset(assets)
        self.assertEqual(asset.download_url, "https://example.com/new.exe")

    def test_no_matching_asset_raises_korean_reason(self):
        with self.assertRaises(UpdateCheckError) as ctx:
            select_asset([{"name": "README.txt", "browser_download_url": "x"}])
        self.assertIn("설치 파일이 없습니다", ctx.exception.reason)


class CheckUpdateTests(unittest.TestCase):
    def test_up_to_date_reports_no_update(self):
        fetch = _fetch_returning({"tag_name": "v1.5.20", "assets": []})
        result = check_update("1.5.20.825", fetch=fetch)
        self.assertFalse(result.is_update_available)
        self.assertEqual(result.latest_version, "v1.5.20")
        self.assertIsNone(result.asset)

    def test_new_release_reports_update_with_asset(self):
        fetch = _fetch_returning({
            "tag_name": "v1.6.0",
            "assets": [
                {
                    "name": "AMBER_1.6.0_x64-setup.exe",
                    "browser_download_url": "https://example.com/setup.exe",
                    "size": 999,
                    "created_at": "2026-03-01T00:00:00Z",
                }
            ],
        })
        result = check_update("1.5.20.825", fetch=fetch)
        self.assertTrue(result.is_update_available)
        self.assertEqual(result.latest_version, "v1.6.0")
        self.assertIsNotNone(result.asset)
        self.assertEqual(result.asset.download_url, "https://example.com/setup.exe")

    def test_new_release_without_asset_raises_korean_reason(self):
        fetch = _fetch_returning({"tag_name": "v1.6.0", "assets": []})
        with self.assertRaises(UpdateCheckError) as ctx:
            check_update("1.5.20.825", fetch=fetch)
        self.assertIn("설치 파일이 없습니다", ctx.exception.reason)

    def test_http_403_reports_rate_limit_reason(self):
        exc = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        fetch = _fetch_raising(exc)
        with self.assertRaises(UpdateCheckError) as ctx:
            check_update("1.5.20.825", fetch=fetch)
        self.assertIn("한도를 초과", ctx.exception.reason)

    def test_http_429_reports_rate_limit_reason(self):
        exc = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)
        fetch = _fetch_raising(exc)
        with self.assertRaises(UpdateCheckError) as ctx:
            check_update("1.5.20.825", fetch=fetch)
        self.assertIn("한도를 초과", ctx.exception.reason)

    def test_other_http_error_reports_status_code(self):
        exc = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
        fetch = _fetch_raising(exc)
        with self.assertRaises(UpdateCheckError) as ctx:
            check_update("1.5.20.825", fetch=fetch)
        self.assertIn("500", ctx.exception.reason)

    def test_timeout_reports_timeout_reason(self):
        fetch = _fetch_raising(TimeoutError("timed out"))
        with self.assertRaises(UpdateCheckError) as ctx:
            check_update("1.5.20.825", fetch=fetch)
        self.assertIn("시간 초과", ctx.exception.reason)

    def test_url_error_reports_network_reason(self):
        exc = urllib.error.URLError("no route to host")
        fetch = _fetch_raising(exc)
        with self.assertRaises(UpdateCheckError) as ctx:
            check_update("1.5.20.825", fetch=fetch)
        self.assertIn("네트워크에 연결할 수 없어", ctx.exception.reason)

    def test_malformed_json_reports_parse_reason(self):
        def _fetch(_request):
            class _BadResponse:
                def read(self):
                    return b"not json"

                def __enter__(self):
                    return self

                def __exit__(self, *exc_info):
                    return False

            return _BadResponse()

        with self.assertRaises(UpdateCheckError) as ctx:
            check_update("1.5.20.825", fetch=_fetch)
        self.assertIn("해석하지 못했습니다", ctx.exception.reason)


class _FakeVersion:
    def __init__(self, version: str):
        self.version = version


class _FakeDownloadResponse:
    """Minimal stand-in for the ``urlopen`` context manager: serves ``data``
    on the first ``read`` call, then signals EOF."""

    def __init__(self, data: bytes):
        self._data = data
        self._served = False

    def read(self, size=-1):
        if not self._served:
            self._served = True
            return self._data
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _ImmediateThread:
    """Replaces ``threading.Thread`` so worker functions run synchronously
    on the calling (test) thread instead of a real background thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class DownloadCompleteDialogTests(unittest.TestCase):
    """UI-level test for the 'download complete' branch: the user must be
    asked whether to install, and declining must not install or delete the
    downloaded file (see overlay/update_check.py)."""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.root.destroy()
        self.tmpdir.cleanup()

    def _flush_pending_after_callbacks(self):
        for _ in range(20):
            self.root.update()

    def test_declining_install_keeps_file_and_reports_its_path(self):
        import overlay.update_check as uc

        asset = ReleaseAsset(
            name="AMBER_1.6.0_x64-setup.exe",
            download_url="https://example.com/setup.exe",
            size=5,
            created_at="2026-01-01T00:00:00Z",
        )
        result = UpdateResult(
            is_update_available=True,
            current_version="1.5.20.825",
            latest_version="v1.6.0",
            asset=asset,
        )

        showinfo_calls = []
        popen_calls = []

        def fake_askyesno(title, message, parent=None):
            if title == "새 버전 발견":
                return True
            if title == "다운로드 완료":
                return False
            raise AssertionError(f"unexpected askyesno title: {title}")

        def fake_showinfo(title, message, parent=None):
            showinfo_calls.append((title, message))

        def fake_popen(*args, **kwargs):
            popen_calls.append((args, kwargs))

        with patch.object(uc, "resolve_version", return_value=_FakeVersion("1.5.20.825")), \
                patch.object(uc, "check_update", return_value=result), \
                patch.object(uc.threading, "Thread", _ImmediateThread), \
                patch.object(uc, "_updates_dir", return_value=Path(self.tmpdir.name)), \
                patch.object(uc.urllib.request, "urlopen", return_value=_FakeDownloadResponse(b"hello")), \
                patch.object(uc.messagebox, "askyesno", side_effect=fake_askyesno), \
                patch.object(uc.messagebox, "showinfo", side_effect=fake_showinfo), \
                patch.object(uc.subprocess, "Popen", side_effect=fake_popen):
            uc.check_for_update(self.root)
            self._flush_pending_after_callbacks()

        target_path = Path(self.tmpdir.name) / asset.name

        # (a) the installer must not be launched
        self.assertEqual(popen_calls, [])

        # (b) the downloaded file must not be deleted
        self.assertTrue(target_path.exists())
        self.assertEqual(target_path.read_bytes(), b"hello")

        # (c) the user must be told where the file is
        self.assertTrue(showinfo_calls)
        last_title, last_message = showinfo_calls[-1]
        self.assertEqual(last_title, "설치 보류")
        self.assertIn(str(target_path), last_message)


if __name__ == "__main__":
    unittest.main()
