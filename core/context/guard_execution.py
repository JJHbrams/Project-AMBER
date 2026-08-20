from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable


_PROTECTED_BRANCHES = {"main", "master", "dev"}
_DEFAULT_GIT_TIMEOUT_SECONDS = 5.0


def _normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except Exception:
            value = [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        normalized = text.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def normalize_action_metadata(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "mode": str(raw.get("mode") or "").strip().lower(),
        "category": str(raw.get("category") or "").strip().lower(),
        "tags": _normalize_string_list(raw.get("tags")),
    }


def normalize_chore_intent(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "is_chore": _normalize_bool(raw.get("is_chore")),
        "summary": str(raw.get("summary") or raw.get("reason") or "").strip(),
    }


def normalize_independent_task_context(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    owner = str(raw.get("existing_changes_owner") or "").strip().lower()
    if owner not in {"same-task", "other-task", "unknown"}:
        owner = "unknown"
    return {
        "requested": _normalize_bool(
            raw.get("requested", raw.get("is_new_independent_task"))
        ),
        "existing_changes_owner": owner,
        "existing_task_id": str(raw.get("existing_task_id") or "").strip(),
        "new_task_id": str(raw.get("new_task_id") or "").strip(),
    }


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _bounded_timeout(timeout_seconds: float | None = None) -> float:
    if timeout_seconds is None:
        return _DEFAULT_GIT_TIMEOUT_SECONDS
    try:
        value = float(timeout_seconds)
    except (TypeError, ValueError):
        return _DEFAULT_GIT_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_GIT_TIMEOUT_SECONDS
    return min(value, 30.0)


def _normalize_cwd(cwd: str) -> str:
    text = str(cwd or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).resolve())
    except Exception:
        return os.path.abspath(text)


def _result(
    guard_id: str,
    status: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "guard_id": guard_id,
        "status": status,
        "reason": reason,
        "evidence": evidence or {},
    }


def _run_git(cwd: str, *args: str, timeout_seconds: float) -> dict[str, Any]:
    command = ["git", *args]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            # 이 guard 는 콘솔이 없는 GUI 프로세스(engram-overlay.exe, agent-policy-hook)
            # 에서도 돈다. 플래그가 없으면 git 이 새 콘솔 창을 띄워 포커스를 뺏는다.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "error_type": "git_not_found",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "command": command,
            "returncode": None,
            "stdout": _decode_output(exc.stdout).strip(),
            "stderr": _decode_output(exc.stderr).strip(),
            "error_type": "timeout",
        }
    except OSError as exc:
        return {
            "ok": False,
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "error_type": "os_error",
        }

    return {
        "ok": completed.returncode == 0,
        "command": command,
        "returncode": completed.returncode,
        "stdout": str(completed.stdout or "").strip(),
        "stderr": str(completed.stderr or "").strip(),
        "error_type": "",
    }


def _resolve_repo_root(cwd: str, *, timeout_seconds: float) -> tuple[str | None, dict[str, Any] | None]:
    normalized_cwd = _normalize_cwd(cwd)
    if not normalized_cwd:
        return None, _result(
            "git-context",
            "error",
            "cwd is required for guard execution",
            {"cwd": ""},
        )
    if not os.path.isdir(normalized_cwd):
        return None, _result(
            "git-context",
            "error",
            "cwd does not exist or is not a directory",
            {"cwd": normalized_cwd},
        )

    repo_cmd = _run_git(
        normalized_cwd,
        "rev-parse",
        "--show-toplevel",
        timeout_seconds=timeout_seconds,
    )
    if not repo_cmd["ok"]:
        return None, _result(
            "git-context",
            "error",
            "failed to resolve git repository root",
            {
                "cwd": normalized_cwd,
                "git": repo_cmd,
            },
        )
    repo_root = str(repo_cmd["stdout"] or "").strip()
    if not repo_root:
        return None, _result(
            "git-context",
            "error",
            "git repository root was empty",
            {
                "cwd": normalized_cwd,
                "git": repo_cmd,
            },
        )
    return repo_root, None


def _execute_protected_branch(
    *,
    cwd: str,
    action_metadata: dict[str, Any],
    chore_intent: dict[str, Any],
    independent_task_context: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    del independent_task_context
    repo_root, repo_error = _resolve_repo_root(cwd, timeout_seconds=timeout_seconds)
    if repo_error is not None:
        return _result(
            "protected-branch",
            "error",
            repo_error["reason"],
            {
                **repo_error["evidence"],
                "action_metadata": action_metadata,
                "chore_intent": chore_intent,
            },
        )

    branch_cmd = _run_git(
        repo_root,
        "rev-parse",
        "--abbrev-ref",
        "HEAD",
        timeout_seconds=timeout_seconds,
    )
    if not branch_cmd["ok"]:
        return _result(
            "protected-branch",
            "error",
            "failed to read current branch",
            {
                "cwd": _normalize_cwd(cwd),
                "repo_root": repo_root,
                "action_metadata": action_metadata,
                "chore_intent": chore_intent,
                "git": branch_cmd,
            },
        )

    branch = str(branch_cmd["stdout"] or "").strip()
    protected_branch = branch.lower() in _PROTECTED_BRANCHES
    is_repo_write = str(action_metadata.get("mode") or "") == "repo-write"
    is_chore = bool(chore_intent.get("is_chore"))
    evidence = {
        "cwd": _normalize_cwd(cwd),
        "repo_root": repo_root,
        "branch": branch,
        "protected_branch": protected_branch,
        "protected_branches": sorted(_PROTECTED_BRANCHES),
        "action_metadata": action_metadata,
        "chore_intent": chore_intent,
        "git": {
            "branch": branch_cmd,
        },
    }

    if not is_repo_write:
        return _result(
            "protected-branch",
            "pass",
            "guard not applicable to non repo-write action",
            evidence,
        )
    if protected_branch and not is_chore:
        return _result(
            "protected-branch",
            "fail",
            "repo-write action targets a protected branch without explicit chore intent",
            evidence,
        )
    if protected_branch and is_chore:
        return _result(
            "protected-branch",
            "pass",
            "explicit structured chore intent allows protected-branch repo-write action",
            evidence,
        )
    return _result(
        "protected-branch",
        "pass",
        "repo-write action is on a non-protected branch",
        evidence,
    )


def _execute_dirty_worktree(
    *,
    cwd: str,
    action_metadata: dict[str, Any],
    chore_intent: dict[str, Any],
    independent_task_context: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    del chore_intent
    repo_root, repo_error = _resolve_repo_root(cwd, timeout_seconds=timeout_seconds)
    if repo_error is not None:
        return _result(
            "dirty-worktree",
            "error",
            repo_error["reason"],
            {
                **repo_error["evidence"],
                "action_metadata": action_metadata,
                "independent_task_context": independent_task_context,
            },
        )

    status_cmd = _run_git(
        repo_root,
        "status",
        "--porcelain",
        "--untracked-files=normal",
        timeout_seconds=timeout_seconds,
    )
    if not status_cmd["ok"]:
        return _result(
            "dirty-worktree",
            "error",
            "failed to inspect worktree status",
            {
                "cwd": _normalize_cwd(cwd),
                "repo_root": repo_root,
                "action_metadata": action_metadata,
                "independent_task_context": independent_task_context,
                "git": status_cmd,
            },
        )

    status_entries = [
        line.rstrip()
        for line in str(status_cmd["stdout"] or "").splitlines()
        if line.strip()
    ]
    status_evidence = {
        **status_cmd,
        "stdout": "\n".join(status_entries[:20]),
        "stdout_truncated": len(status_entries) > 20,
    }
    requested = bool(independent_task_context.get("requested")) or "new-independent-task" in set(
        action_metadata.get("tags") or []
    )
    existing_changes_owner = str(independent_task_context.get("existing_changes_owner") or "unknown")
    dirty = bool(status_entries)
    evidence = {
        "cwd": _normalize_cwd(cwd),
        "repo_root": repo_root,
        "dirty": dirty,
        "dirty_entry_count": len(status_entries),
        "status_entries": status_entries[:20],
        "requested_new_independent_task": requested,
        "existing_changes_owner": existing_changes_owner,
        "independent_task_context": independent_task_context,
        "action_metadata": action_metadata,
        "git": {
            "status": status_evidence,
        },
    }

    if not dirty:
        return _result(
            "dirty-worktree",
            "pass",
            "worktree is clean",
            evidence,
        )
    if not requested:
        return _result(
            "dirty-worktree",
            "pass",
            "no explicit new independent task request was supplied",
            evidence,
        )
    if existing_changes_owner != "other-task":
        return _result(
            "dirty-worktree",
            "pass",
            "existing changes were not explicitly assigned to another task",
            evidence,
        )
    return _result(
        "dirty-worktree",
        "fail",
        "dirty worktree already contains changes explicitly owned by another task",
        evidence,
    )


_GUARD_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "protected-branch": _execute_protected_branch,
    "dirty-worktree": _execute_dirty_worktree,
}


def execute_guard(
    guard_id: str,
    *,
    cwd: str = "",
    action_metadata: dict[str, Any] | None = None,
    chore_intent: dict[str, Any] | None = None,
    independent_task_context: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    normalized_guard_id = str(guard_id or "").strip()
    normalized_action_metadata = normalize_action_metadata(action_metadata)
    normalized_chore_intent = normalize_chore_intent(chore_intent)
    normalized_independent_task_context = normalize_independent_task_context(independent_task_context)
    bounded_timeout = _bounded_timeout(timeout_seconds)

    if not normalized_guard_id:
        return _result(
            normalized_guard_id,
            "error",
            "guard_id is required",
            {
                "cwd": _normalize_cwd(cwd),
                "action_metadata": normalized_action_metadata,
                "chore_intent": normalized_chore_intent,
                "independent_task_context": normalized_independent_task_context,
            },
        )

    executor = _GUARD_REGISTRY.get(normalized_guard_id)
    if executor is None:
        return _result(
            normalized_guard_id,
            "error",
            "unknown guard id",
            {
                "cwd": _normalize_cwd(cwd),
                "action_metadata": normalized_action_metadata,
                "chore_intent": normalized_chore_intent,
                "independent_task_context": normalized_independent_task_context,
            },
        )

    return executor(
        cwd=cwd,
        action_metadata=normalized_action_metadata,
        chore_intent=normalized_chore_intent,
        independent_task_context=normalized_independent_task_context,
        timeout_seconds=bounded_timeout,
    )
