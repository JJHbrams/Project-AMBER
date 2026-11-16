import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.install import job_object

ROOT = Path(__file__).resolve().parents[1]
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


def _wait_exit(proc, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_closing_job_kills_assigned_child_only():
    job = job_object.create_kill_on_close_job()
    assert job
    assigned = subprocess.Popen(SLEEP, creationflags=subprocess.CREATE_NO_WINDOW)
    bystander = subprocess.Popen(SLEEP, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert job_object.assign_to_job(job, assigned, "test")
        job_object.close_job(job)
        assert _wait_exit(assigned, 10), "assigned child must die when the job closes"
        assert bystander.poll() is None, "unassigned process must survive"
    finally:
        for proc in (assigned, bystander):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def test_create_returns_none_when_unavailable(monkeypatch):
    import ctypes
    monkeypatch.setattr(job_object.sys, "platform", "linux")
    assert job_object.create_kill_on_close_job() is None
    monkeypatch.setattr(job_object.sys, "platform", "win32")

    def boom(*args, **kwargs):
        raise OSError("no kernel32")
    monkeypatch.setattr(ctypes, "WinDLL", boom, raising=False)
    assert job_object.create_kill_on_close_job() is None


def test_assign_never_raises():
    class Broken:
        pid = 1
        @property
        def _handle(self):
            raise RuntimeError("no handle")
    assert job_object.assign_to_job(None, Broken()) is False
    assert job_object.assign_to_job(123, None) is False
    assert job_object.assign_to_job(123, Broken()) is False


def test_restart_spawn_path_is_not_assigned():
    source = (ROOT / "overlay" / "main.py").read_text(encoding="utf-8")
    spawn = re.search(r"def _spawn\(\):(.*?)threading\.Timer", source, re.S)
    assert spawn, "restart _spawn not found"
    code = "\n".join(l for l in spawn.group(1).splitlines() if not l.strip().startswith("#"))
    assert "_assign_service_child" not in code and "assign_to_job" not in code
    for role in ("mcp-server", "dashboard", "kg-watcher"):
        assert f'_assign_service_child(proc, "{role}")' in source
