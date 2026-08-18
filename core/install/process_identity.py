"""Exact Windows process identity checks for Engram development restarts."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable


_PROCESS_NAMES = {
    "python.exe",
    "pythonw.exe",
    "streamlit.exe",
    "engram-overlay.exe",
    "engram-dashboard.exe",
}


def _tokens(command_line: str) -> list[str]:
    try:
        return [token.strip('"') for token in shlex.split(command_line, posix=False)]
    except ValueError:
        return []


def same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))
    except (OSError, TypeError, ValueError):
        return False


def trusted_installed_paths() -> dict[str, tuple[Path, ...]]:
    roots: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Programs" / "EngramOverlay")
    for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value) / "EngramOverlay")
    return {
        "overlay": tuple(root / "dist" / "engram-overlay" / "engram-overlay.exe" for root in roots),
        "dashboard": tuple(root / "dist" / "engram-overlay" / "engram-dashboard.exe" for root in roots),
    }


def is_default_installed_frozen_child(identity: dict[str, Any]) -> bool:
    executable = str(identity.get("ExecutablePath") or "")
    command = _tokens(str(identity.get("CommandLine") or ""))
    if not executable or not command or not same_path(command[0], executable):
        return False
    trusted = trusted_installed_paths()
    if any(same_path(executable, path) for path in trusted["dashboard"]):
        return len(command) >= 1 and not any(token.casefold() == "--role" for token in command)
    if not any(same_path(executable, path) for path in trusted["overlay"]):
        return False
    role_indexes = [index for index, token in enumerate(command) if token.casefold() == "--role"]
    return (
        len(role_indexes) == 1
        and role_indexes[0] + 1 < len(command)
        and command[role_indexes[0] + 1].casefold() in {"mcp-server", "kg-watcher"}
    )


def is_same_checkout_source_child(identity: dict[str, Any], source_root: Path) -> bool:
    executable = str(identity.get("ExecutablePath") or "")
    command = _tokens(str(identity.get("CommandLine") or ""))
    if not executable or len(command) < 2 or not same_path(command[0], executable):
        return False
    executable_name = Path(executable).name.casefold()
    backend_scripts = (
        (source_root / "mcp_server.py").resolve(),
        (source_root / "scripts" / "kg" / "kg_watcher.py").resolve(),
    )
    if executable_name in {"python.exe", "pythonw.exe"} and any(
        any(same_path(token, script) for token in command[1:]) for script in backend_scripts
    ):
        return True
    dashboard = (source_root / "scripts" / "engram_dashboard.py").resolve()
    return executable_name in {"python.exe", "pythonw.exe", "streamlit.exe"} and any(
        same_path(token, dashboard) for token in command[1:]
    )


def _run_powershell(script: str) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip()


def _normalize_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "ProcessId": int(value.get("ProcessId", 0) or 0),
        "ParentProcessId": int(value.get("ParentProcessId", 0) or 0),
        "Name": str(value.get("Name") or ""),
        "ExecutablePath": str(value.get("ExecutablePath") or ""),
        "CommandLine": str(value.get("CommandLine") or ""),
    }


def list_candidate_processes() -> list[dict[str, Any]]:
    names = ",".join(f"'{name}'" for name in sorted(_PROCESS_NAMES))
    script = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        f"$names=@({names}); Get-CimInstance Win32_Process | "
        "Where-Object { $names -contains $_.Name.ToLowerInvariant() } | "
        "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        raw = _run_powershell(script)
        if not raw:
            return []
        payload = json.loads(raw)
        values = payload if isinstance(payload, list) else [payload]
        return [_normalize_identity(value) for value in values if isinstance(value, dict)]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
        return []


def get_process_identity(pid: int) -> dict[str, Any] | None:
    script = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        f"Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\" | "
        "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        raw = _run_powershell(script)
        payload = json.loads(raw)
        identity = _normalize_identity(payload)
        return identity if identity["ProcessId"] == int(pid) else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _same_process(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return (
        int(first.get("ProcessId", 0)) == int(second.get("ProcessId", 0))
        and same_path(str(first.get("ExecutablePath") or ""), str(second.get("ExecutablePath") or ""))
        and str(first.get("CommandLine") or "") == str(second.get("CommandLine") or "")
    )


def terminate_identity_exact(
    identity: dict[str, Any],
    predicate: Callable[[dict[str, Any]], bool],
) -> bool:
    pid = int(identity.get("ProcessId", 0) or 0)
    if pid <= 0 or pid == os.getpid():
        return False
    current = get_process_identity(pid)
    if current is None or not _same_process(identity, current) or not predicate(current):
        return False
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
        if not handle:
            return False
        try:
            return bool(ctypes.windll.kernel32.TerminateProcess(handle, 0))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return False


def cleanup_dev_restart_orphans(source_root: Path) -> list[int]:
    stopped: list[int] = []
    for identity in list_candidate_processes():
        predicate = lambda value: (
            is_default_installed_frozen_child(value)
            or is_same_checkout_source_child(value, source_root)
        )
        if predicate(identity) and terminate_identity_exact(identity, predicate):
            stopped.append(int(identity["ProcessId"]))
    return stopped


def snapshot_source_children(parent_pid: int, source_root: Path) -> list[dict[str, Any]]:
    return [
        identity
        for identity in list_candidate_processes()
        if int(identity.get("ParentProcessId", 0)) == int(parent_pid)
        and is_same_checkout_source_child(identity, source_root)
    ]


def cleanup_source_snapshot(snapshot: list[dict[str, Any]], source_root: Path) -> list[int]:
    stopped: list[int] = []
    predicate = lambda value: is_same_checkout_source_child(value, source_root)
    for identity in snapshot:
        if predicate(identity) and terminate_identity_exact(identity, predicate):
            stopped.append(int(identity["ProcessId"]))
    return stopped


def cleanup_same_checkout_source_orphans(source_root: Path) -> list[int]:
    stopped: list[int] = []
    predicate = lambda value: is_same_checkout_source_child(value, source_root)
    for identity in list_candidate_processes():
        if predicate(identity) and terminate_identity_exact(identity, predicate):
            stopped.append(int(identity["ProcessId"]))
    return stopped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exact Engram child process cleanup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--parent-pid", type=int, required=True)
    snapshot_parser.add_argument("--source-root", type=Path, required=True)
    snapshot_parser.add_argument("--output", type=Path, required=True)
    cleanup_parser = subparsers.add_parser("cleanup-snapshot")
    cleanup_parser.add_argument("--source-root", type=Path, required=True)
    cleanup_parser.add_argument("--snapshot", type=Path, required=True)
    source_cleanup_parser = subparsers.add_parser("cleanup-source-orphans")
    source_cleanup_parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        payload = snapshot_source_children(args.parent_pid, args.source_root.resolve())
        args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return 0
    if args.command == "cleanup-source-orphans":
        stopped = cleanup_same_checkout_source_orphans(args.source_root.resolve())
        print(json.dumps({"stopped": stopped}, sort_keys=True))
        return 0
    payload = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot.is_file() else []
    stopped = cleanup_source_snapshot(payload if isinstance(payload, list) else [], args.source_root.resolve())
    print(json.dumps({"stopped": stopped}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
