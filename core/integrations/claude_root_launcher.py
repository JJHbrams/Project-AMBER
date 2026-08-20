"""Exact-owner launcher for Engram's root Claude CLI shim.

Only this launcher registers a process; overlay deliberately never discovers
arbitrary claude.exe processes globally.
"""
import ctypes
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from typing import Sequence

from core.storage.db import get_connection


def _process_creation_identity(pid: int) -> str:
    if os.name != "nt":
        return f"pid:{pid}:started:{time.time_ns()}"
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OSError("cannot open root Claude child")
    try:
        created = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise OSError("GetProcessTimes failed")
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def launch_root_claude(argv: Sequence[str], *, cwd: str, bootstrap: str) -> int:
    token = secrets.token_urlsafe(24)
    env = os.environ.copy()
    env["ENGRAM_ROOT_CLIENT_TOKEN"] = token
    # Keep the token inside the tool call's argument list; a trailing prose
    # suffix is not executable MCP instruction.
    env["ENGRAM_BOOTSTRAP"] = re.sub(
        r"(engram_get_context_once\([^)]*?)\)",
        lambda match: f"{match.group(1)}, client_token='{token}')",
        bootstrap,
        count=1,
    )
    child_argv = list(argv)
    if "--append-system-prompt" in child_argv:
        index = child_argv.index("--append-system-prompt") + 1
        if index < len(child_argv):
            child_argv[index] = env["ENGRAM_BOOTSTRAP"]
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "INSERT INTO root_cli_owners(client_token,pid,creation_identity,started_at,ended_at,status,session_id) VALUES(?,0,'',?,NULL,'launching',NULL)",
                (token, time.time()),
            )
    finally:
        conn.close()
    try:
        child = subprocess.Popen(child_argv, cwd=cwd, env=env)
    except Exception:
        conn = get_connection()
        try:
            with conn:
                conn.execute(
                    "UPDATE root_cli_owners SET ended_at=?,status='launch_failed' "
                    "WHERE client_token=? AND status='launching'",
                    (time.time(), token),
                )
        finally:
            conn.close()
        raise
    try:
        creation_id = _process_creation_identity(child.pid)
    except Exception:
        child.terminate()
        child.wait()
        conn = get_connection()
        try:
            with conn:
                conn.execute(
                    "UPDATE root_cli_owners SET pid=?,ended_at=?,status='identity_failed' "
                    "WHERE client_token=? AND status='launching'",
                    (child.pid, time.time(), token),
                )
        finally:
            conn.close()
        return 3
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE root_cli_owners SET pid=?,creation_identity=?,status='running' WHERE client_token=? AND status='launching'",
                (child.pid, creation_id, token),
            )
    finally:
        conn.close()
    exit_code = child.wait()
    # The session may be unbound when the CLI never called get_context_once.
    # In that case do not close by scope or by newest-session heuristic.
    conn = get_connection()
    try:
        owner = conn.execute(
            "SELECT session_id FROM root_cli_owners "
            "WHERE client_token=? AND creation_identity=? AND status='running'",
            (token, creation_id),
        ).fetchone()
        row = None
        if owner and owner["session_id"]:
            row = conn.execute(
                "SELECT id,scope_key FROM sessions WHERE id=? AND root_client_token=? AND ended_at IS NULL",
                (int(owner["session_id"]), token),
            ).fetchone()
    finally:
        conn.close()
    if row:
        # Exact local broker close; never fall back to scope/database close.
        port = int(os.environ.get("ENGRAM_STM_PORT", "17384"))
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/stm/session/close",
            data=json.dumps({
                "session_id": int(row["id"]), "scope_key": str(row["scope_key"]),
                "summary": "Root Claude CLI 종료", "journal_origin": "root",
            }).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15):
                pass
        except Exception:
            conn = get_connection()
            try:
                with conn:
                    conn.execute(
                        "UPDATE root_cli_owners SET ended_at=?,status='close_failed' "
                        "WHERE client_token=? AND creation_identity=? AND status='running'",
                        (time.time(), token, creation_id),
                    )
            finally:
                conn.close()
            return 3
        final_status = "closed"
    else:
        final_status = "exited_unbound"
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                "UPDATE root_cli_owners SET ended_at=?,status=? "
                "WHERE client_token=? AND creation_identity=? AND status='running'",
                (time.time(), final_status, token, creation_id),
            )
    finally:
        conn.close()
    return int(exit_code)


def main(argv: Sequence[str]) -> int:
    if not argv:
        return 2
    cwd = os.getcwd()
    bootstrap = os.environ.get("ENGRAM_BOOTSTRAP", "")
    return launch_root_claude(argv, cwd=cwd, bootstrap=bootstrap)
