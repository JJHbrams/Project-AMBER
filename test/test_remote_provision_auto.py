"""터널 연결 시 원격 배치를 자동 갱신하는 경로 검증.

의도한 동작은 VS Code Remote-SSH 와 같다 — 접속이 수립되면 원격 쪽 물건이 최신으로
맞춰진다. 다만 백그라운드에서 ssh 를 띄우는 일이라 다음 두 가지를 반드시 지켜야 한다.

- **바뀐 게 없으면 ssh 를 아예 띄우지 않는다.** 재연결마다 원격 홈에 쓰면 안 된다.
- **한 번도 등록되지 않은 호스트는 건드리지 않는다.** 첫 배치는 토큰 전송과 터널
  실측이 있는 `setup-remote.ps1` 의 일이고, 그걸 조용히 백그라운드에서 할 수는 없다.
"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.integrations import remote_provision as rp
from overlay.remote_tunnel import STATE_CONNECTING, STATE_DOWN, STATE_UP, TunnelManager, _Tunnel

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _StatePathPatch:
    """상태 파일을 임시 경로로 돌린다 — 실제 ~/.engram 을 건드리지 않는다."""

    def __init__(self):
        self._tmp = TemporaryDirectory()
        self._patch = patch.object(
            rp, "_STATE_PATH", Path(self._tmp.name) / "remote-provisioned.json"
        )

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        self._tmp.cleanup()
        return False


class SkillsRootTests(unittest.TestCase):
    def test_finds_repo_root_from_module_location(self):
        self.assertEqual(rp.resolve_skills_root(), _REPO_ROOT)

    def test_explicit_root_without_skills_is_rejected(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(rp.resolve_skills_root(tmp))


class FingerprintTests(unittest.TestCase):
    def test_same_content_gives_same_fingerprint(self):
        a = rp.build_remote_payload(_REPO_ROOT, remote_os="posix")
        b = rp.build_remote_payload(_REPO_ROOT, remote_os="posix")
        self.assertEqual(rp.payload_fingerprint(a), rp.payload_fingerprint(b))

    def test_changed_skill_body_changes_fingerprint(self):
        base = rp.build_remote_payload(_REPO_ROOT, remote_os="posix")
        drifted = dict(base)
        drifted["skills"] = dict(base["skills"])
        first = sorted(drifted["skills"])[0]
        drifted["skills"][first] = drifted["skills"][first] + "\n추가된 절차\n"
        self.assertNotEqual(rp.payload_fingerprint(base), rp.payload_fingerprint(drifted))

    def test_remote_os_changes_fingerprint(self):
        posix = rp.build_remote_payload(_REPO_ROOT, remote_os="posix")
        windows = rp.build_remote_payload(_REPO_ROOT, remote_os="windows")
        self.assertNotEqual(rp.payload_fingerprint(posix), rp.payload_fingerprint(windows))


class ProvisionHostTests(unittest.TestCase):
    def test_unregistered_host_is_left_alone(self):
        with _StatePathPatch(), patch.object(rp.subprocess, "run") as run:
            result = rp.provision_host("never-set-up")
            self.assertTrue(result["skipped"])
            self.assertFalse(result["ok"])
            run.assert_not_called()

    def test_unchanged_fingerprint_skips_ssh_entirely(self):
        with _StatePathPatch():
            payload = rp.build_remote_payload(_REPO_ROOT, remote_os="posix")
            rp.record_provisioned(
                "host-a",
                fingerprint=rp.payload_fingerprint(payload),
                remote_python="/usr/bin/python3",
                remote_os="posix",
            )
            with patch.object(rp.subprocess, "run") as run:
                result = rp.provision_host("host-a")
                self.assertTrue(result["ok"])
                self.assertTrue(result["skipped"])
                self.assertEqual(result["reason"], "unchanged")
                run.assert_not_called()

    def test_drifted_fingerprint_runs_ssh_and_records_new_one(self):
        with _StatePathPatch():
            rp.record_provisioned(
                "host-b",
                fingerprint="staleeeeeeeeeeee",
                remote_python="/usr/bin/python3",
                remote_os="posix",
            )

            class _Ok:
                returncode = 0
                stdout = "SKILL=x\nPROVISIONED\n"
                stderr = ""

            with patch.object(rp.subprocess, "run", return_value=_Ok()) as run:
                result = rp.provision_host("host-b")
                self.assertTrue(result["ok"])
                self.assertFalse(result["skipped"])
                run.assert_called_once()
                # 백그라운드다 — 비밀번호를 물을 수 없고, 콘솔 창을 띄워서도 안 된다.
                cmd = run.call_args.args[0]
                self.assertIn("BatchMode=yes", cmd)
                self.assertIn("creationflags", run.call_args.kwargs)

            self.assertEqual(
                rp.load_records()["host-b"]["fingerprint"],
                rp.payload_fingerprint(rp.build_remote_payload(_REPO_ROOT, remote_os="posix")),
            )

    def test_windows_automatic_refresh_uses_short_framed_stdin_transport(self):
        with _StatePathPatch():
            rp.record_provisioned("host-win", fingerprint="stale", remote_python=r"C:\Windows\py.exe", remote_os="windows")

            class _Ok:
                returncode = 0
                stdout = "PROVISIONED\n"
                stderr = ""

            with patch.object(rp.subprocess, "run", return_value=_Ok()) as run:
                result = rp.provision_host("host-win")
            self.assertTrue(result["ok"])
            command = run.call_args.args[0]
            self.assertLess(len(" ".join(command)), 32767)
            self.assertNotIn(rp.encode_payload_script(), " ".join(command))
            frame = run.call_args.kwargs["input"]
            self.assertIn('"installer"', frame)
            self.assertIn('"payload"', frame)

    def test_failed_ssh_does_not_record_success(self):
        with _StatePathPatch():
            rp.record_provisioned(
                "host-c",
                fingerprint="staleeeeeeeeeeee",
                remote_python="/usr/bin/python3",
                remote_os="posix",
            )

            class _Fail:
                returncode = 255
                stdout = ""
                stderr = "Permission denied (publickey).\n"

            with patch.object(rp.subprocess, "run", return_value=_Fail()):
                result = rp.provision_host("host-c")
            self.assertFalse(result["ok"])
            self.assertIn("Permission denied", str(result["reason"]))
            # 실패했으면 지문을 갱신하지 않는다 — 다음 연결에서 다시 시도해야 한다.
            self.assertEqual(rp.load_records()["host-c"]["fingerprint"], "staleeeeeeeeeeee")

    def test_ssh_exception_is_reported_not_raised(self):
        with _StatePathPatch():
            rp.record_provisioned(
                "host-d",
                fingerprint="staleeeeeeeeeeee",
                remote_python="/usr/bin/python3",
                remote_os="posix",
            )
            with patch.object(rp.subprocess, "run", side_effect=OSError("boom")):
                result = rp.provision_host("host-d")
            self.assertFalse(result["ok"])
            self.assertIn("boom", str(result["reason"]))

    def test_callback_swallows_every_failure(self):
        # 배치 실패가 터널 상태에 영향을 주면 안 된다.
        with patch.object(rp, "provision_host", side_effect=RuntimeError("nope")):
            rp.refresh_host_on_tunnel_up("host-e")  # 예외가 새어 나오면 실패


class SshCommandTests(unittest.TestCase):
    def test_posix_uses_single_quotes(self):
        inner = rp._ssh_command("h", "/usr/bin/python3", "QUJD", "posix")[-1]
        self.assertTrue(inner.startswith("'/usr/bin/python3'"))

    def test_windows_uses_proven_cmd_wrapper_for_powershell_login_shell(self):
        inner = rp._ssh_command("h", r"C:\py\python.exe", "QUJD", "windows")[-1]
        self.assertEqual(
            inner,
            'cmd.exe /d /s /c """C:\\py\\python.exe"" -c ""import base64;exec(base64.b64decode(\'QUJD\'))"""',
        )


class TunnelUpCallbackTests(unittest.TestCase):
    def _manager(self, calls: list[str], done: threading.Event) -> TunnelManager:
        def cb(host: str) -> None:
            calls.append(host)
            done.set()

        return TunnelManager(get_port=lambda: 17386, on_tunnel_up=cb)

    def test_fires_once_on_transition_to_up(self):
        calls: list[str] = []
        done = threading.Event()
        manager = self._manager(calls, done)
        tunnel = _Tunnel(host="h1")
        tunnel.status.state = STATE_CONNECTING

        manager._set_state(tunnel, STATE_UP)
        self.assertTrue(done.wait(5), "콜백이 호출되지 않았다")

        # 이미 UP 인 상태에서 다시 UP 으로 세팅해도 전이가 아니므로 호출되지 않는다.
        done.clear()
        manager._set_state(tunnel, STATE_UP)
        self.assertFalse(done.wait(0.5), "전이가 아닌데 콜백이 호출됐다")
        self.assertEqual(calls, ["h1"])

    def test_does_not_fire_for_other_states(self):
        calls: list[str] = []
        done = threading.Event()
        manager = self._manager(calls, done)
        tunnel = _Tunnel(host="h2")

        for state in (STATE_CONNECTING, STATE_DOWN):
            manager._set_state(tunnel, state)
        self.assertFalse(done.wait(0.5))
        self.assertEqual(calls, [])

    def test_reconnect_after_down_fires_again(self):
        # 끊겼다 다시 붙는 것은 새 전이다 — 그때는 갱신 여부를 다시 판정해야 한다.
        calls: list[str] = []
        done = threading.Event()
        manager = self._manager(calls, done)
        tunnel = _Tunnel(host="h3")

        manager._set_state(tunnel, STATE_UP)
        self.assertTrue(done.wait(5))
        done.clear()
        manager._set_state(tunnel, STATE_DOWN)
        manager._set_state(tunnel, STATE_UP)
        self.assertTrue(done.wait(5))
        self.assertEqual(calls, ["h3", "h3"])

    def test_no_callback_configured_is_safe(self):
        manager = TunnelManager(get_port=lambda: 17386)
        manager._set_state(_Tunnel(host="h4"), STATE_UP)  # 예외 없이 넘어가야 한다


if __name__ == "__main__":
    unittest.main()
