from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from core.config.runtime_config import normalize_policy_guidance_level
from core.integrations.engram_bootstrap import (
    _policy_preflight_backend_command_parts,
    get_policy_guidance_level,
)
from core.integrations.policy_preflight import process_policy_request


MANAGED_MARKER = "engram managed pre-commit policy hook"
POWERSHELL_HOOK_NAME = "engram-pre-commit.ps1"
POLICY_STATE_NAME = "engram-repo-policy.json"
OPT_OUT_MARKER_NAME = "engram-repo-policy.opt-out"
POLICY_LOCK_NAME = "engram-repo-policy.lock"
POLICY_LOCK_STALE_SECONDS = 300


class GitHookError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            # 콘솔 없는 overlay/hook 프로세스에서 호출돼도 콘솔 창이 뜨지 않게 한다.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitHookError("git-unavailable", f"Git command failed: {exc}") from exc
    if check and completed.returncode != 0:
        reason = (completed.stderr or completed.stdout or "Git command failed").strip()
        raise GitHookError("git-error", reason, returncode=completed.returncode)
    return completed


def _resolve_repository(repo_path: str | os.PathLike[str]) -> tuple[Path, Path]:
    requested = Path(repo_path).expanduser()
    if not requested.exists() or not requested.is_dir():
        raise GitHookError("invalid-repository", f"Repository path does not exist: {requested}")
    requested = requested.resolve()
    root_result = _run_git(requested, "rev-parse", "--show-toplevel")
    root_text = root_result.stdout.strip()
    if not root_text:
        raise GitHookError("invalid-repository", f"Not a Git worktree: {requested}")
    repo_root = Path(root_text).resolve()
    common_result = _run_git(repo_root, "rev-parse", "--git-common-dir")
    common_dir = Path(common_result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repo_root / common_dir
    return repo_root, common_dir.resolve()


def _custom_hooks_path(repo_root: Path) -> str:
    result = _run_git(repo_root, "config", "--get", "core.hooksPath", check=False)
    if result.returncode == 1:
        return ""
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "Unable to read core.hooksPath").strip()
        raise GitHookError("git-config-error", reason, returncode=result.returncode)
    return result.stdout.strip()


def _is_managed(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        return MANAGED_MARKER in path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(tmp_path, path)
        try:
            path.chmod(path.stat().st_mode | 0o111)
        except OSError:
            pass
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _policy_paths(common_dir: Path) -> tuple[Path, Path, Path]:
    return (
        common_dir / POLICY_STATE_NAME,
        common_dir / OPT_OUT_MARKER_NAME,
        common_dir / POLICY_LOCK_NAME,
    )


@contextmanager
def _repo_policy_lock(common_dir: Path):
    _state_path, _opt_out_path, lock_path = _policy_paths(common_dir)
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError as exc:
            try:
                stale = time.time() - lock_path.stat().st_mtime >= POLICY_LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if attempt == 0 and stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            raise GitHookError(
                "policy-busy",
                "Repository policy update is already in progress.",
                lock_path=str(lock_path),
            ) from exc
    if descriptor is None:
        raise GitHookError("policy-busy", "Repository policy lock could not be acquired.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(f"pid={os.getpid()}\n")
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _read_policy_state(common_dir: Path) -> dict[str, Any] | None:
    state_path, _opt_out_path, _lock_path = _policy_paths(common_dir)
    if not state_path.exists():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_policy_state(common_dir: Path, state: dict[str, Any]) -> None:
    state_path, _opt_out_path, _lock_path = _policy_paths(common_dir)
    _write_text_atomic(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def _local_config_values(repo_root: Path, key: str) -> list[str]:
    result = _run_git(repo_root, "config", "--local", "--get-all", key, check=False)
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or f"Unable to read {key}").strip()
        raise GitHookError("git-config-error", reason, returncode=result.returncode)
    return [line for line in result.stdout.splitlines() if line != ""]


def _replace_local_config_values(repo_root: Path, key: str, values: list[str]) -> None:
    cleared = _run_git(repo_root, "config", "--local", "--unset-all", key, check=False)
    if cleared.returncode not in {0, 5}:
        reason = (cleared.stderr or cleared.stdout or f"Unable to clear {key}").strip()
        raise GitHookError("git-config-error", reason, returncode=cleared.returncode)
    for value in values:
        _run_git(repo_root, "config", "--local", "--add", key, value)


def _set_managed_merge_ff(repo_root: Path) -> None:
    _run_git(repo_root, "config", "--local", "--replace-all", "merge.ff", "false")


def _previous_merge_ff_values(state: dict[str, Any] | None, current: list[str]) -> list[str]:
    if isinstance(state, dict):
        merge_state = state.get("merge_ff")
        if isinstance(merge_state, dict) and isinstance(merge_state.get("previous_values"), list):
            return [str(value) for value in merge_state["previous_values"]]
    return list(current)


def _restore_managed_merge_ff(repo_root: Path, state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict):
        return False
    merge_state = state.get("merge_ff")
    if not isinstance(merge_state, dict) or not isinstance(merge_state.get("previous_values"), list):
        return False
    previous_values = [str(value) for value in merge_state["previous_values"]]
    if _local_config_values(repo_root, "merge.ff") == previous_values:
        return False
    _replace_local_config_values(repo_root, "merge.ff", previous_values)
    return True


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sh_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _render_shell_hook() -> str:
    backend_exe, backend_args = _policy_preflight_backend_command_parts()
    role_args = list(backend_args)
    if len(role_args) >= 2 and role_args[-2:] == ["--role", "policy-preflight"]:
        role_args[-1] = "git-hook"
    else:
        role_args.extend(["--role", "git-hook"])
    command = " ".join(
        _sh_single_quote(part)
        for part in (
            backend_exe,
            *role_args,
            "advise",
            "--repo",
        )
    )
    return (
        "#!/bin/sh\n"
        f"# {MANAGED_MARKER}\n"
        "# Advisory only: every launcher and backend failure must allow the commit.\n"
        "if [ -n \"${ENGRAM_POLICY_GUIDANCE_DISABLED_FILE:-}\" ]; then\n"
        "  disabled_marker=$ENGRAM_POLICY_GUIDANCE_DISABLED_FILE\n"
        "elif [ -n \"${HOME:-}\" ]; then\n"
        "  disabled_marker=$HOME/.engram/policy-guidance.disabled\n"
        "else\n"
        "  disabled_marker=\n"
        "fi\n"
        "if [ -n \"$disabled_marker\" ] && [ -f \"$disabled_marker\" ]; then exit 0; fi\n"
        "repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || {\n"
        "  echo 'Engram policy guidance unavailable: repository root could not be resolved.' >&2\n"
        "  exit 0\n"
        "}\n"
        "advisor_timeout=${ENGRAM_POLICY_GUIDANCE_TIMEOUT_SECONDS:-10}\n"
        "if command -v timeout >/dev/null 2>&1; then\n"
        f"  timeout \"$advisor_timeout\" {command} \"$repo_root\"\n"
        "  advisor_status=$?\n"
        "else\n"
        f"  {command} \"$repo_root\"\n"
        "  advisor_status=$?\n"
        "fi\n"
        "if [ \"$advisor_status\" -ne 0 ]; then\n"
        "  echo 'Engram policy guidance unavailable: advisor backend failed or timed out.' >&2\n"
        "fi\n"
        "exit 0\n"
    )


def _render_powershell_hook() -> str:
    backend_exe, backend_args = _policy_preflight_backend_command_parts()
    rendered_args = ", ".join(_ps_single_quote(arg) for arg in backend_args)
    return (
        f"# {MANAGED_MARKER}\n"
        "# Opt-in repository advisor. It reports and audits policy risk but never blocks commit.\n"
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n"
        f"$backendExe = {_ps_single_quote(backend_exe)}\n"
        f"$backendArgs = @({rendered_args})\n"
        "$requestPath = Join-Path ([System.IO.Path]::GetTempPath()) ('engram-git-policy-' + [System.Guid]::NewGuid().ToString('N') + '.json')\n"
        "$responsePath = Join-Path ([System.IO.Path]::GetTempPath()) ('engram-git-policy-response-' + [System.Guid]::NewGuid().ToString('N') + '.json')\n"
        "$errorPath = Join-Path ([System.IO.Path]::GetTempPath()) ('engram-git-policy-error-' + [System.Guid]::NewGuid().ToString('N') + '.log')\n"
        "$responseJson = ''\n"
        "$backendError = ''\n"
        "$backendExitCode = 1\n"
        "$decision = ''\n"
        "$reason = ''\n"
        "try {\n"
        "  $repoRoot = (& git rev-parse --show-toplevel 2>$null | Select-Object -First 1)\n"
        "  if (-not $repoRoot) { throw 'Unable to resolve repository root.' }\n"
        "  $repoRoot = [string]$repoRoot.Trim()\n"
        "  $isChore = ([string]$env:ENGRAM_CHORE_INTENT).Trim() -eq '1'\n"
        "  $choreReason = ([string]$env:ENGRAM_CHORE_REASON).Trim()\n"
        "  $request = @{\n"
        "    request_type = 'preflight'\n"
        "    caller = 'git-hook'\n"
        "    cwd = $repoRoot\n"
        "    action = 'git-commit'\n"
        "    user_query = 'git commit'\n"
        "    action_metadata = @{ mode = 'repo-write'; category = 'git-hook'; tags = @('git', 'commit', 'policy-guidance') }\n"
        "    chore_intent = @{ is_chore = $isChore; reason = $choreReason }\n"
        "    execute_guards = $true\n"
        "    persist_audit = $true\n"
        "    advisory_only = $true\n"
        "  } | ConvertTo-Json -Depth 6 -Compress\n"
        "  [System.IO.File]::WriteAllText($requestPath, $request, [System.Text.UTF8Encoding]::new($false))\n"
        "  $backendArgLine = ((@($backendArgs) + @('--request-file', $requestPath, '--response-file', $responsePath, '--error-file', $errorPath)) | ForEach-Object {\n"
        "    if ($_ -match '[\\s\"]') { '\"' + ($_.Replace('\"', '\"\"')) + '\"' } else { [string]$_ }\n"
        "  }) -join ' '\n"
        "  $backendProcess = Start-Process -FilePath $backendExe -ArgumentList $backendArgLine -Wait -PassThru\n"
        "  if (Test-Path $responsePath) {\n"
        "    $responseJson = [System.IO.File]::ReadAllText($responsePath, [System.Text.UTF8Encoding]::new($false)).Trim()\n"
        "  }\n"
        "  if (Test-Path $errorPath) {\n"
        "    $backendError = [System.IO.File]::ReadAllText($errorPath, [System.Text.UTF8Encoding]::new($false)).Trim()\n"
        "  }\n"
        "  $backendExitCode = if ($null -eq $backendProcess) { 1 } else { [int]$backendProcess.ExitCode }\n"
        "} catch {\n"
        "  $reason = $_.Exception.Message\n"
        "} finally {\n"
        "  if (Test-Path $requestPath) { Remove-Item $requestPath -Force -ErrorAction SilentlyContinue }\n"
        "  if (Test-Path $responsePath) { Remove-Item $responsePath -Force -ErrorAction SilentlyContinue }\n"
        "  if (Test-Path $errorPath) { Remove-Item $errorPath -Force -ErrorAction SilentlyContinue }\n"
        "}\n"
        "if ($responseJson) {\n"
        "  try {\n"
        "    $parsed = $responseJson | ConvertFrom-Json\n"
        "    if ($parsed -and $parsed.PSObject.Properties['decision']) { $decision = [string]$parsed.decision }\n"
        "    if ($parsed -and $parsed.PSObject.Properties['reason']) { $reason = [string]$parsed.reason }\n"
        "  } catch {\n"
        "    if (-not $reason) { $reason = 'Engram policy response was invalid.' }\n"
        "  }\n"
        "}\n"
        "if ($backendExitCode -eq 0 -and $decision -eq 'allow') { exit 0 }\n"
        "if (-not $reason -and $backendError) { $reason = [string](($backendError -split \"`r?`n\")[0]) }\n"
        "if (-not $reason) { $reason = 'Engram policy evaluation failed.' }\n"
        "[Console]::Error.WriteLine('Engram policy guidance: ' + $reason)\n"
        "exit 0\n"
    )


def git_hook_status(repo_path: str | os.PathLike[str]) -> dict[str, Any]:
    repo_root, common_dir = _resolve_repository(repo_path)
    hooks_dir = common_dir / "hooks"
    hook_path = hooks_dir / "pre-commit"
    powershell_path = hooks_dir / POWERSHELL_HOOK_NAME
    state_path, opt_out_path, _lock_path = _policy_paths(common_dir)
    custom_hooks_path = _custom_hooks_path(repo_root)
    wrapper_managed = _is_managed(hook_path)
    powershell_managed = _is_managed(powershell_path)
    conflict = hook_path.exists() and not wrapper_managed
    installed = wrapper_managed
    state = _read_policy_state(common_dir)
    merge_state = state.get("merge_ff") if isinstance(state, dict) else None
    merge_ff_active = bool(isinstance(merge_state, dict) and merge_state.get("active"))
    merge_ff_values = _local_config_values(repo_root, "merge.ff")
    return {
        "ok": True,
        "operation": "status",
        "repository": str(repo_root),
        "common_dir": str(common_dir),
        "hooks_dir": str(hooks_dir),
        "installed": installed,
        "active": installed and not custom_hooks_path and merge_ff_active and merge_ff_values == ["false"],
        "conflict": conflict,
        "custom_hooks_path": custom_hooks_path,
        "legacy_helper_present": powershell_path.exists(),
        "legacy_helper_managed": powershell_managed,
        "opted_out": opt_out_path.exists(),
        "policy_state_present": state_path.exists(),
        "merge_ff_managed": merge_ff_active,
        "merge_ff_values": merge_ff_values,
    }


def _activate_repo_policy(repo_root: Path, common_dir: Path, *, clear_opt_out: bool) -> dict[str, Any]:
    status = git_hook_status(repo_root)
    hooks_dir = Path(status["hooks_dir"])
    if status["custom_hooks_path"]:
        raise GitHookError(
            "custom-hooks-path",
            "Refusing to install because core.hooksPath is already configured.",
            repository=str(repo_root),
            core_hooks_path=status["custom_hooks_path"],
        )
    if status["conflict"]:
        raise GitHookError(
            "existing-hook",
            "Refusing to overwrite an existing non-Engram pre-commit hook or helper.",
            repository=str(repo_root),
        )
    hook_path = hooks_dir / "pre-commit"
    powershell_path = hooks_dir / POWERSHELL_HOOK_NAME
    state_path, opt_out_path, _lock_path = _policy_paths(common_dir)
    before_hook = hook_path.read_text(encoding="utf-8") if hook_path.exists() else None
    before_state = state_path.read_text(encoding="utf-8") if state_path.exists() else None
    current_merge_values = _local_config_values(repo_root, "merge.ff")
    previous_state = _read_policy_state(common_dir)
    previous_merge_values = _previous_merge_ff_values(previous_state, current_merge_values)
    shell_content = _render_shell_hook()
    state = {
        "version": 1,
        "merge_ff": {
            "active": True,
            "previous_values": previous_merge_values,
        },
    }
    try:
        _set_managed_merge_ff(repo_root)
        _write_text_atomic(hook_path, shell_content)
        _write_policy_state(common_dir, state)
    except Exception:
        try:
            _replace_local_config_values(repo_root, "merge.ff", current_merge_values)
        except Exception:
            pass
        try:
            if before_hook is None:
                if _is_managed(hook_path):
                    hook_path.unlink(missing_ok=True)
            else:
                _write_text_atomic(hook_path, before_hook)
        except Exception:
            pass
        try:
            if before_state is None:
                state_path.unlink(missing_ok=True)
            else:
                _write_text_atomic(state_path, before_state)
        except Exception:
            pass
        raise
    legacy_removed = False
    if powershell_path.exists() and _is_managed(powershell_path):
        powershell_path.unlink()
        legacy_removed = True
    opt_out_removed = False
    if clear_opt_out and opt_out_path.exists():
        opt_out_path.unlink()
        opt_out_removed = True
    changed = (
        before_hook != shell_content
        or current_merge_values != ["false"]
        or before_state != json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        or legacy_removed
        or opt_out_removed
    )
    return {
        "ok": True,
        "operation": "install",
        "repository": str(repo_root),
        "hooks_dir": str(hooks_dir),
        "installed": True,
        "changed": changed,
        "opted_out": False,
        "merge_ff_values": ["false"],
    }


def install_git_hook(repo_path: str | os.PathLike[str]) -> dict[str, Any]:
    repo_root, common_dir = _resolve_repository(repo_path)
    with _repo_policy_lock(common_dir):
        return _activate_repo_policy(repo_root, common_dir, clear_opt_out=True)


def uninstall_git_hook(repo_path: str | os.PathLike[str]) -> dict[str, Any]:
    repo_root, common_dir = _resolve_repository(repo_path)
    hooks_dir = common_dir / "hooks"
    state_path, opt_out_path, _lock_path = _policy_paths(common_dir)
    with _repo_policy_lock(common_dir):
        _write_text_atomic(opt_out_path, "Automatic Engram repository policy installation disabled by user.\n")
        state = _read_policy_state(common_dir)
        merge_ff_restored = _restore_managed_merge_ff(repo_root, state)
        removed: list[str] = []
        preserved: list[str] = []
        for path in (hooks_dir / "pre-commit", hooks_dir / POWERSHELL_HOOK_NAME):
            if not path.exists():
                continue
            if _is_managed(path):
                path.unlink()
                removed.append(path.name)
            else:
                preserved.append(path.name)
        state_path.unlink(missing_ok=True)
    return {
        "ok": True,
        "operation": "uninstall",
        "repository": str(repo_root),
        "hooks_dir": str(hooks_dir),
        "installed": False,
        "removed": removed,
        "preserved": preserved,
        "opted_out": True,
        "merge_ff_restored": merge_ff_restored,
    }


def ensure_repo_policy(
    repo_path: str | os.PathLike[str],
    *,
    guidance_level: str | None = None,
) -> dict[str, Any]:
    """Idempotently synchronize repository policy during session bootstrap.

    This is deliberately fail-open: non-repositories, conflicts, read-only Git dirs,
    and concurrent bootstrap attempts return a skipped result instead of blocking the
    caller's session.
    """
    level = normalize_policy_guidance_level(
        guidance_level if guidance_level is not None else get_policy_guidance_level()
    )
    try:
        repo_root, common_dir = _resolve_repository(repo_path)
        state_path, opt_out_path, _lock_path = _policy_paths(common_dir)
        if level == "off":
            state = _read_policy_state(common_dir)
            if not isinstance(state, dict):
                return {
                    "ok": True,
                    "operation": "ensure",
                    "repository": str(repo_root),
                    "skipped": True,
                    "reason": "guidance-off",
                }
            with _repo_policy_lock(common_dir):
                changed = _restore_managed_merge_ff(repo_root, state)
                merge_state = state.get("merge_ff")
                if isinstance(merge_state, dict):
                    merge_state["active"] = False
                _write_policy_state(common_dir, state)
            return {
                "ok": True,
                "operation": "ensure",
                "repository": str(repo_root),
                "changed": changed,
                "enabled": False,
                "merge_ff_restored": changed,
            }
        if opt_out_path.exists():
            return {
                "ok": True,
                "operation": "ensure",
                "repository": str(repo_root),
                "skipped": True,
                "reason": "user-opt-out",
                "opted_out": True,
            }
        with _repo_policy_lock(common_dir):
            result = _activate_repo_policy(repo_root, common_dir, clear_opt_out=False)
        result["operation"] = "ensure"
        result["enabled"] = True
        return result
    except (GitHookError, OSError) as exc:
        return {
            "ok": True,
            "operation": "ensure",
            "skipped": True,
            "reason": exc.code if isinstance(exc, GitHookError) else "filesystem-error",
            "detail": str(exc),
        }


def manage_git_hook(operation: str, repo_path: str | os.PathLike[str]) -> dict[str, Any]:
    normalized = str(operation or "").strip().lower()
    if normalized == "install":
        return install_git_hook(repo_path)
    if normalized == "uninstall":
        return uninstall_git_hook(repo_path)
    if normalized == "status":
        return git_hook_status(repo_path)
    raise GitHookError("invalid-operation", f"Unsupported Git hook operation: {operation}")


def advise_git_commit(repo_path: str | os.PathLike[str]) -> None:
    """Evaluate and report commit risk without ever rejecting the commit."""
    try:
        repo_root, _hooks_dir = _resolve_repository(repo_path)
        result, _exit_code, _stderr = process_policy_request(
            {
                "request_type": "preflight",
                "caller": "git-hook",
                "cwd": str(repo_root),
                "action": "git-commit",
                "user_query": "git commit",
                "action_metadata": {
                    "mode": "repo-write",
                    "category": "git-hook",
                    "tags": ["git", "commit", "policy-guidance"],
                },
                "chore_intent": {
                    "is_chore": str(os.environ.get("ENGRAM_CHORE_INTENT", "")).strip() == "1",
                    "reason": str(os.environ.get("ENGRAM_CHORE_REASON", "")).strip(),
                },
                "execute_guards": True,
                "persist_audit": True,
                "advisory_only": True,
            }
        )
        if str(result.get("decision") or "").lower() == "allow":
            return
        reason = str(result.get("reason") or "Engram policy evaluation failed.").strip()
        print(f"Engram policy guidance: {reason}", file=sys.stderr)
    except BaseException as exc:
        reason = str(exc).strip() or "advisor backend failed"
        print(f"Engram policy guidance unavailable: {reason}", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Manage the opt-in Engram repository pre-commit hook")
    parser.add_argument("operation", choices=("install", "status", "uninstall", "advise"))
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    if args.operation == "advise":
        advise_git_commit(args.repo)
        return
    try:
        result = manage_git_hook(args.operation, args.repo)
    except GitHookError as exc:
        result = {"ok": False, "operation": args.operation, "code": exc.code, "error": str(exc), **exc.details}
        print(_compact_json(result))
        raise SystemExit(1)
    except OSError as exc:
        result = {"ok": False, "operation": args.operation, "code": "filesystem-error", "error": str(exc)}
        print(_compact_json(result))
        raise SystemExit(1)
    print(_compact_json(result))


if __name__ == "__main__":
    main()
