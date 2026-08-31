from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from core.config.runtime_config import get_cfg_value, normalize_policy_guidance_level
from core.context.directives import preflight_directives
from core.context.project_scope import resolve_project_key, resolve_scope_key


ALLOW_EXIT_CODE = 0
ERROR_EXIT_CODE = 1
BLOCKED_EXIT_CODE = 2

_CLAUDE_HOOK_REQUEST_TYPE = "claude-pretool-hook"
_CODEX_HOOK_REQUEST_TYPE = "codex-pretool-hook"
_COPILOT_HOOK_REQUEST_TYPE = "copilot-pretool-hook"
_ANTIGRAVITY_HOOK_REQUEST_TYPE = "antigravity-pretool-hook"
_AGENT_HOOK_REQUEST_TYPES = {
    _CLAUDE_HOOK_REQUEST_TYPE: "claude-code",
    _CODEX_HOOK_REQUEST_TYPE: "codex",
    _COPILOT_HOOK_REQUEST_TYPE: "copilot",
    _ANTIGRAVITY_HOOK_REQUEST_TYPE: "antigravity",
}
_AGENT_HOOK_PROVIDER_LABELS = {
    "claude-code": "Claude",
    "codex": "Codex",
    "copilot": "Copilot",
    "antigravity": "Antigravity",
}
_WRITE_TOOL_NAMES = {"write", "edit", "multiedit", "notebookedit"}
_COMMAND_TOOL_NAMES = {"bash", "powershell"}
# Provider tool names that mean the same repo-write operation as the canonical names above.
# Keyed by the lowercased tool name each CLI reports in its pre-tool hook payload.
_TOOL_NAME_ALIASES = {
    # shell execution
    "shell": "bash",
    "run_shell_command": "bash",
    "run_command": "bash",
    "execute_bash": "bash",
    "terminal": "bash",
    "pwsh": "powershell",
    # file writes
    "write_file": "write",
    "create_file": "write",
    "createfile": "write",
    "str_replace": "edit",
    "str_replace_editor": "edit",
    "edit_file": "edit",
    "replace": "edit",
    # Antigravity PreToolUse reports camel-case toolCall names and args.
    "write_to_file": "write",
    "replace_file_content": "edit",
    "multi_replace_file_content": "multiedit",
}
_WRITE_TOOL_PATH_FIELDS = {
    "write": ("file_path", "path", "absolute_path", "TargetFile", "targetFile"),
    "edit": ("file_path", "path", "absolute_path", "TargetFile", "targetFile"),
    "multiedit": ("file_path", "path", "TargetFile", "targetFile"),
    "notebookedit": ("notebook_path", "file_path", "path", "notebookPath"),
}
_GIT_EXECUTABLE_NAMES = {"git", "git.exe", "git.cmd", "git.bat"}
_GIT_WRITE_SUBCOMMANDS = {"commit", "merge", "rebase", "cherry-pick"}
_GIT_READONLY_SUBCOMMANDS = {"status", "log", "diff", "show", "branch", "rev-parse"}
_GIT_UNSAFE_READ_OPTIONS = {"--ext-diff", "--textconv"}
_GIT_UNSAFE_READ_ENV = {"GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS"}
_GIT_NON_WRITING_SUBCOMMANDS = {"help", "version"}
_GIT_GLOBAL_OPTIONS_NO_VALUE = {
    "--bare",
    "--literal-pathspecs",
    "--no-literal-pathspecs",
    "--no-optional-locks",
    "--no-pager",
    "--no-replace-objects",
    "--paginate",
}
_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "--config-env",
    "--exec-path",
    "--git-dir",
    "--namespace",
    "--super-prefix",
    "--work-tree",
}
_GIT_INLINE_VALUE_PREFIXES = (
    "--config-env=",
    "--exec-path=",
    "--git-dir=",
    "--namespace=",
    "--super-prefix=",
    "--work-tree=",
)
_GIT_TERMINAL_GLOBAL_OPTIONS = {"--help", "--html-path", "--info-path", "--man-path", "--version", "-h"}
_GIT_SHELL_WRAPPERS = {
    "bash": "bash",
    "bash.exe": "bash",
    "cmd": "cmd",
    "cmd.exe": "cmd",
    "powershell": "powershell",
    "powershell.exe": "powershell",
    "pwsh": "powershell",
    "pwsh.exe": "powershell",
    "sh": "bash",
    "sh.exe": "bash",
}
_GIT_CONTROL_PREFIXES = {
    "case",
    "do",
    "done",
    "else",
    "elseif",
    "for",
    "foreach",
    "if",
    "switch",
    "then",
    "until",
    "while",
}
_GIT_BRANCH_READONLY_OPTIONS_NO_VALUE = {
    "--all",
    "--ignore-case",
    "--list",
    "--no-abbrev",
    "--no-color",
    "--omit-empty",
    "--quiet",
    "--remotes",
    "--show-current",
    "--verbose",
    "-a",
    "-q",
    "-r",
    "-v",
    "-vv",
}
_GIT_BRANCH_READONLY_OPTIONS_WITH_VALUE = {
    "--abbrev",
    "--contains",
    "--format",
    "--merged",
    "--no-contains",
    "--no-merged",
    "--points-at",
    "--sort",
}
_GIT_BRANCH_READONLY_OPTION_PREFIXES = (
    "--abbrev=",
    "--column=",
    "--contains=",
    "--format=",
    "--merged=",
    "--no-contains=",
    "--no-merged=",
    "--points-at=",
    "--sort=",
)
_GIT_BRANCH_MUTATING_OPTIONS = {
    "--copy",
    "--create-reflog",
    "--delete",
    "--edit-description",
    "--force",
    "--move",
    "--set-upstream-to",
    "--track",
    "--unset-upstream",
    "-C",
    "-D",
    "-M",
    "-c",
    "-d",
    "-f",
    "-l",
    "-m",
    "-u",
}
_GIT_BRANCH_MUTATING_OPTION_PREFIXES = (
    "--copy=",
    "--delete=",
    "--move=",
    "--set-upstream-to=",
    "--track=",
    "-u=",
)
_MAX_NESTED_GIT_SHELL_DEPTH = 4


class HookPayloadError(ValueError):
    pass


def _reconfigure_std_streams() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def _compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _write_text_atomic(path_text: str, content: str) -> None:
    target = Path(path_text)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _error_output(reason: str, *, request_type: str, extras: dict[str, Any] | None = None) -> tuple[dict[str, Any], int, str]:
    output: dict[str, Any] = {
        "decision": "error",
        "exit_code": ERROR_EXIT_CODE,
        "reason": reason,
        "request_type": request_type,
    }
    if extras:
        output.update(extras)
    return output, ERROR_EXIT_CODE, reason


def _find_git_worktree_root(cwd: str) -> str | None:
    text = _normalize_text(cwd)
    if not text:
        return None
    try:
        path = Path(text).resolve()
    except OSError:
        return None
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def _require_directory(path_text: str) -> str:
    text = _normalize_text(path_text)
    if not text:
        raise HookPayloadError("hook cwd is required for repo-write policy evaluation")
    try:
        resolved = Path(text).resolve()
    except OSError:
        raise HookPayloadError("hook cwd is not a valid directory")
    if not resolved.exists() or not resolved.is_dir():
        raise HookPayloadError("hook cwd does not exist or is not a directory")
    return str(resolved)


def _extract_tool_name(payload: dict[str, Any]) -> str:
    for key in ("tool_name", "tool", "name"):
        text = _normalize_text(payload.get(key))
        if text:
            return text
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        return _normalize_text(tool_call.get("name"))
    return ""


def _extract_tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("tool_input", "input", "toolInput"):
        value = payload.get(key)
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except Exception:
                return {"command": text}
            if isinstance(parsed, dict):
                return dict(parsed)
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        args = tool_call.get("args")
        if isinstance(args, dict):
            return dict(args)
    return {}


def _extract_bash_command(payload: dict[str, Any], tool_input: dict[str, Any]) -> str:
    for candidate in (
        tool_input.get("command"), tool_input.get("cmd"), tool_input.get("CommandLine"),
        payload.get("command"), payload.get("cmd"), payload.get("CommandLine"),
    ):
        text = _normalize_text(candidate)
        if text:
            return text
    return ""


def _path_values(value: Any) -> list[str]:
    if isinstance(value, str):
        text = _normalize_text(value)
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            text = _normalize_text(item)
            if text:
                values.append(text)
        return values
    return []


def _extract_write_target_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for field_name in _WRITE_TOOL_PATH_FIELDS.get(tool_name, ()):
        for path_text in _path_values(tool_input.get(field_name)):
            if path_text in seen:
                continue
            seen.add(path_text)
            paths.append(path_text)
    return paths


def _resolve_tool_target_path(path_text: str, cwd: str) -> Path:
    text = _normalize_text(path_text)
    if not text:
        raise HookPayloadError("tool target path is required for repo-write policy evaluation")
    raw_path = Path(text)
    try:
        if raw_path.is_absolute():
            return raw_path.resolve()
        base_dir = Path(_require_directory(cwd))
        return (base_dir / raw_path).resolve()
    except OSError:
        raise HookPayloadError("tool target path is not valid")


def _nearest_existing_directory(path: Path) -> Path | None:
    candidate = path if path.exists() and path.is_dir() else path.parent
    for current in (candidate, *candidate.parents):
        if current.exists() and current.is_dir():
            return current
    return None


def _split_shell_command_segments(command: str, shell_type: str) -> list[str]:
    if not command:
        return []
    normalized_shell = _normalize_text(shell_type).lower()
    segments: list[str] = []
    buffer: list[str] = []
    quote = ""
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        next_char = command[index + 1] if index + 1 < length else ""
        if quote:
            buffer.append(char)
            if normalized_shell == "powershell":
                if quote == "'" and char == "'" and next_char == "'":
                    buffer.append(next_char)
                    index += 2
                    continue
                if quote == '"' and char == "`" and next_char:
                    buffer.append(next_char)
                    index += 2
                    continue
                if char == quote:
                    quote = ""
                index += 1
                continue
            if quote == '"' and char == "\\" and next_char:
                buffer.append(next_char)
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buffer.append(char)
            index += 1
            continue
        if normalized_shell == "powershell" and char == "`" and next_char:
            buffer.append(char)
            buffer.append(next_char)
            index += 2
            continue
        if normalized_shell != "powershell" and char == "\\" and next_char:
            buffer.append(char)
            buffer.append(next_char)
            index += 2
            continue
        if char == "\r":
            if next_char == "\n":
                index += 1
            segment = "".join(buffer).strip()
            if segment:
                segments.append(segment)
            buffer = []
            index += 1
            continue
        if char in {"\n", ";"}:
            segment = "".join(buffer).strip()
            if segment:
                segments.append(segment)
            buffer = []
            index += 1
            continue
        if char == "&" and next_char == "&":
            segment = "".join(buffer).strip()
            if segment:
                segments.append(segment)
            buffer = []
            index += 2
            continue
        if normalized_shell == "cmd" and char == "&":
            segment = "".join(buffer).strip()
            if segment:
                segments.append(segment)
            buffer = []
            index += 1
            continue
        if char == "|" and next_char == "|":
            segment = "".join(buffer).strip()
            if segment:
                segments.append(segment)
            buffer = []
            index += 2
            continue
        if char == "|":
            segment = "".join(buffer).strip()
            if segment:
                segments.append(segment)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    segment = "".join(buffer).strip()
    if segment:
        segments.append(segment)
    return segments


def _tokenize_shell_command_segment(segment: str, shell_type: str) -> list[str]:
    tokens: list[str] = []
    buffer: list[str] = []
    quote = ""
    normalized_shell = _normalize_text(shell_type).lower()
    index = 0
    length = len(segment)
    while index < length:
        char = segment[index]
        next_char = segment[index + 1] if index + 1 < length else ""
        if quote:
            if normalized_shell == "powershell":
                if quote == "'" and char == "'" and next_char == "'":
                    buffer.append("'")
                    index += 2
                    continue
                if quote == '"' and char == "`" and next_char:
                    buffer.append(next_char)
                    index += 2
                    continue
                if char == quote:
                    quote = ""
                    index += 1
                    continue
                buffer.append(char)
                index += 1
                continue
            if quote == '"' and char == "\\" and next_char:
                buffer.append(next_char)
                index += 2
                continue
            if char == quote:
                quote = ""
                index += 1
                continue
            buffer.append(char)
            index += 1
            continue
        if char in {" ", "\t"}:
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if normalized_shell == "powershell" and char == "`" and next_char:
            buffer.append(next_char)
            index += 2
            continue
        if normalized_shell != "powershell" and char == "\\" and next_char:
            buffer.append(next_char)
            index += 2
            continue
        buffer.append(char)
        index += 1
    if buffer:
        tokens.append("".join(buffer))
    return tokens


def _is_git_executable(token: str) -> bool:
    if not token:
        return False
    return Path(token).name.lower() in _GIT_EXECUTABLE_NAMES


def _resolve_existing_path(path_text: str, base_dir: Path | None, *, require_directory: bool) -> Path | None:
    text = _normalize_text(path_text)
    if not text:
        return None
    try:
        raw_path = Path(text)
        if raw_path.is_absolute():
            resolved = raw_path.resolve()
        else:
            if base_dir is None:
                return None
            resolved = (base_dir / raw_path).resolve()
    except OSError:
        return None
    if not resolved.exists():
        return None
    if require_directory and not resolved.is_dir():
        return None
    return resolved


def _apply_git_change_directory(current_dir: Path | None, current_dir_known: bool, path_text: str) -> tuple[Path | None, bool]:
    resolved = _resolve_existing_path(
        path_text,
        current_dir if current_dir_known else None,
        require_directory=True,
    )
    if resolved is None:
        return current_dir, False
    return resolved, True


def _derive_git_worktree_from_git_dir(git_dir: Path | None) -> Path | None:
    if git_dir is None or git_dir.name != ".git":
        return None
    parent = git_dir.parent
    if not parent.exists() or not parent.is_dir():
        return None
    return parent.resolve()


def _parse_bash_env_assignment(token: str) -> tuple[str, str] | None:
    name, separator, value = token.partition("=")
    if not separator or not name:
        return None
    if not (name[0].isalpha() or name[0] == "_"):
        return None
    if any(not (char.isalnum() or char == "_") for char in name[1:]):
        return None
    return name, value


def _store_git_env_override(overrides: dict[str, str], name: str, value: str) -> None:
    normalized_name = _normalize_text(name).upper()
    if normalized_name in {"GIT_DIR", "GIT_WORK_TREE", *_GIT_UNSAFE_READ_ENV}:
        overrides[normalized_name] = value


def _clear_git_env_override(overrides: dict[str, str], name: str) -> None:
    normalized_name = _normalize_text(name).upper()
    if normalized_name in {"GIT_DIR", "GIT_WORK_TREE", *_GIT_UNSAFE_READ_ENV}:
        overrides.pop(normalized_name, None)


def _strip_bash_safe_prefixes(
    tokens: list[str],
    current_dir: Path | None,
    current_dir_known: bool,
) -> tuple[list[str], dict[str, str], Path | None, bool] | None:
    command_env: dict[str, str] = {}
    effective_dir = current_dir
    effective_dir_known = current_dir_known
    index = 0
    while True:
        while index < len(tokens):
            assignment = _parse_bash_env_assignment(tokens[index])
            if assignment is None:
                break
            name, value = assignment
            _store_git_env_override(command_env, name, value)
            index += 1
        if index >= len(tokens):
            return [], command_env, effective_dir, effective_dir_known
        token = tokens[index]
        if token == "env":
            index += 1
            while index < len(tokens):
                token = tokens[index]
                assignment = _parse_bash_env_assignment(token)
                if assignment is not None:
                    name, value = assignment
                    _store_git_env_override(command_env, name, value)
                    index += 1
                    continue
                if token == "--":
                    index += 1
                    break
                if token in {"-0", "--null", "-i", "--ignore-environment"}:
                    index += 1
                    continue
                if token in {"-u", "--unset"}:
                    if index + 1 >= len(tokens):
                        return None
                    _clear_git_env_override(command_env, tokens[index + 1])
                    index += 2
                    continue
                if token.startswith("--unset="):
                    _clear_git_env_override(command_env, token.split("=", 1)[1])
                    index += 1
                    continue
                if token in {"-C", "--chdir"}:
                    if index + 1 >= len(tokens):
                        return None
                    effective_dir, effective_dir_known = _apply_git_change_directory(
                        effective_dir,
                        effective_dir_known,
                        tokens[index + 1],
                    )
                    index += 2
                    continue
                if token.startswith("--chdir="):
                    effective_dir, effective_dir_known = _apply_git_change_directory(
                        effective_dir,
                        effective_dir_known,
                        token.split("=", 1)[1],
                    )
                    index += 1
                    continue
                if token.startswith("-"):
                    return None
                break
            continue
        if token == "command":
            index += 1
            while index < len(tokens):
                token = tokens[index]
                if token == "--":
                    index += 1
                    break
                if token == "-p":
                    index += 1
                    continue
                if token in {"-v", "-V"}:
                    return [], command_env, effective_dir, effective_dir_known
                if token.startswith("-"):
                    return None
                break
            continue
        break
    return tokens[index:], command_env, effective_dir, effective_dir_known


def _parse_powershell_env_assignment_segment(tokens: list[str]) -> tuple[str, str] | None:
    if not tokens or not tokens[0].lower().startswith("$env:"):
        return None
    first = tokens[0][5:]
    if len(tokens) == 1:
        name, separator, value = first.partition("=")
        if not separator:
            return None
        return name, value
    if len(tokens) == 3 and tokens[1] == "=":
        return first, tokens[2]
    return None


def _extract_bash_change_directory_target(tokens: list[str]) -> tuple[bool, str | None]:
    if not tokens or tokens[0] != "cd":
        return False, None
    args = tokens[1:]
    if not args:
        return True, None
    if args[0] == "--":
        if len(args) < 2:
            return True, None
        return True, args[1]
    if args[0].startswith("-"):
        return True, None
    return True, args[0]


def _extract_powershell_change_directory_target(tokens: list[str]) -> tuple[bool, str | None]:
    if not tokens:
        return False, None
    command_name = tokens[0].lower()
    if command_name not in {"cd", "set-location"}:
        return False, None
    args = tokens[1:]
    if not args:
        return True, None
    first_arg = args[0]
    normalized_arg = first_arg.lower()
    if normalized_arg in {"-path", "-literalpath"}:
        if len(args) < 2:
            return True, None
        return True, args[1]
    if normalized_arg.startswith("-path:") or normalized_arg.startswith("-literalpath:"):
        return True, first_arg.split(":", 1)[1]
    if first_arg.startswith("-"):
        return True, None
    return True, first_arg


def _apply_shell_change_directory_segment(
    tokens: list[str],
    shell_type: str,
    shell_state: dict[str, Any],
) -> bool:
    normalized_shell = _normalize_text(shell_type).lower()
    if normalized_shell == "powershell":
        matched, path_text = _extract_powershell_change_directory_target(tokens)
    else:
        matched, path_text = _extract_bash_change_directory_target(tokens)
    if not matched:
        return False
    current_dir = shell_state.get("current_dir")
    current_dir_known = bool(shell_state.get("current_dir_known"))
    if path_text is None:
        shell_state["current_dir_known"] = False
        return True
    resolved_dir, resolved_known = _apply_git_change_directory(current_dir, current_dir_known, path_text)
    if resolved_known:
        shell_state["current_dir"] = resolved_dir
    shell_state["current_dir_known"] = resolved_known
    return True


def _initialize_shell_state(cwd: str) -> dict[str, Any]:
    try:
        current_dir = Path(_require_directory(cwd))
    except HookPayloadError:
        current_dir = None
    return {
        "current_dir": current_dir,
        "current_dir_known": current_dir is not None,
        "git_env": {},
    }


def _resolve_git_write_context(
    subcommand: str,
    *,
    current_dir: Path | None,
    current_dir_known: bool,
    git_dir: Path | None,
    git_dir_error: bool,
    work_tree: Path | None,
    work_tree_error: bool,
    env_overrides: dict[str, str],
) -> dict[str, Any]:
    base_dir = current_dir if current_dir_known else None
    if work_tree is None and "GIT_WORK_TREE" in env_overrides:
        work_tree = _resolve_existing_path(
            env_overrides["GIT_WORK_TREE"],
            base_dir,
            require_directory=True,
        )
        if work_tree is None:
            work_tree_error = True
    if git_dir is None and "GIT_DIR" in env_overrides:
        git_dir = _resolve_existing_path(
            env_overrides["GIT_DIR"],
            base_dir,
            require_directory=False,
        )
        if git_dir is None:
            git_dir_error = True
    if work_tree_error:
        raise HookPayloadError("git write command work-tree could not be resolved")
    if git_dir_error and work_tree is None:
        raise HookPayloadError("git write command git-dir could not be resolved")
    if work_tree is not None:
        repo_root = work_tree.resolve()
        request_cwd = str(repo_root)
    elif git_dir is not None:
        repo_root = _derive_git_worktree_from_git_dir(git_dir)
        if repo_root is None:
            raise HookPayloadError("git write command uses --git-dir without an unambiguous work-tree")
        request_cwd = str(repo_root)
    else:
        if not current_dir_known or current_dir is None:
            raise HookPayloadError("git write command working directory could not be resolved")
        repo_root_text = _find_git_worktree_root(str(current_dir))
        if repo_root_text is None:
            raise HookPayloadError("git write command does not target a git worktree")
        repo_root = Path(repo_root_text)
        request_cwd = str(current_dir)
    return {
        "git_write_subcommand": subcommand,
        "git_command_cwd": str(current_dir.resolve()) if current_dir is not None and current_dir_known else "",
        "git_worktree_root": str(repo_root.resolve()),
        "request_cwd": request_cwd,
    }


def _contains_git_executable_token(tokens: list[str]) -> bool:
    return any(_is_git_executable(token) for token in tokens)


def _clone_shell_state(
    current_dir: Path | None,
    current_dir_known: bool,
    env_overrides: dict[str, str],
) -> dict[str, Any]:
    return {
        "current_dir": current_dir,
        "current_dir_known": current_dir_known,
        "git_env": dict(env_overrides),
    }


def _extract_nested_shell_wrapper_command(tokens: list[str]) -> tuple[str, str] | None:
    if not tokens:
        return None
    wrapper_shell = _GIT_SHELL_WRAPPERS.get(Path(tokens[0]).name.lower())
    if wrapper_shell is None:
        return None
    args = tokens[1:]
    if wrapper_shell == "cmd":
        for index, token in enumerate(args):
            normalized = token.lower()
            if normalized in {"/c", "/k"}:
                return wrapper_shell, " ".join(args[index + 1 :]).strip()
        return None
    if wrapper_shell == "powershell":
        for index, token in enumerate(args):
            normalized = token.lower()
            if normalized in {"-command", "-c"}:
                return wrapper_shell, " ".join(args[index + 1 :]).strip()
            if normalized.startswith("-command:") or normalized.startswith("-c:"):
                return wrapper_shell, token.split(":", 1)[1].strip()
        return None
    if wrapper_shell == "bash":
        for index, token in enumerate(args):
            normalized = token.lower()
            if normalized in {"-c", "-lc"}:
                return wrapper_shell, " ".join(args[index + 1 :]).strip()
        return None
    return None


def _validate_git_branch_readonly_arguments(args: list[str]) -> None:
    allow_patterns = False
    arg_index = 0
    while arg_index < len(args):
        token = args[arg_index]
        normalized = token.lower()
        if (
            normalized in _GIT_BRANCH_MUTATING_OPTIONS
            or any(normalized.startswith(prefix) for prefix in _GIT_BRANCH_MUTATING_OPTION_PREFIXES)
        ):
            raise HookPayloadError("git branch command is not a safe read-only operation")
        if normalized in _GIT_BRANCH_READONLY_OPTIONS_NO_VALUE:
            if normalized == "--list":
                allow_patterns = True
            arg_index += 1
            continue
        if normalized in _GIT_BRANCH_READONLY_OPTIONS_WITH_VALUE:
            if arg_index + 1 >= len(args):
                raise HookPayloadError("git branch command is missing an option value")
            arg_index += 2
            continue
        if any(normalized.startswith(prefix) for prefix in _GIT_BRANCH_READONLY_OPTION_PREFIXES):
            arg_index += 1
            continue
        if token == "--" or token.startswith("-"):
            raise HookPayloadError("git branch command could not be classified safely")
        if not allow_patterns:
            raise HookPayloadError("git branch command is not a safe read-only operation")
        arg_index += 1


def _validate_git_readonly_context(
    subcommand: str,
    args: list[str],
    *,
    config_overrides: bool,
    env_overrides: dict[str, str],
) -> None:
    if config_overrides:
        raise HookPayloadError(
            f"git {subcommand} with config overrides is not a safe read-only operation"
        )
    if _GIT_UNSAFE_READ_ENV.intersection(env_overrides):
        raise HookPayloadError(
            f"git {subcommand} with external diff environment is not a safe read-only operation"
        )
    if any(str(arg).lower() in _GIT_UNSAFE_READ_OPTIONS for arg in args):
        raise HookPayloadError(
            f"git {subcommand} can execute an external diff helper"
        )


def _classify_direct_git_command(
    command_tokens: list[str],
    *,
    current_dir: Path | None,
    current_dir_known: bool,
    env_overrides: dict[str, str],
) -> dict[str, str]:
    args = command_tokens[1:]
    git_dir: Path | None = None
    work_tree: Path | None = None
    git_dir_error = False
    work_tree_error = False
    config_overrides = False
    arg_index = 0
    while arg_index < len(args):
        token = args[arg_index]
        normalized = token.lower()
        if normalized in _GIT_WRITE_SUBCOMMANDS:
            result = _resolve_git_write_context(
                normalized,
                current_dir=current_dir,
                current_dir_known=current_dir_known,
                git_dir=git_dir,
                git_dir_error=git_dir_error,
                work_tree=work_tree,
                work_tree_error=work_tree_error,
                env_overrides=env_overrides,
            )
            if normalized == "merge":
                merge_args = [str(value).lower() for value in args[arg_index + 1 :]]
                has_no_ff = "--no-ff" in merge_args
                has_ff_only = "--ff-only" in merge_args
                result["merge_has_no_ff"] = has_no_ff
                result["merge_has_ff_only"] = has_ff_only
                result["merge_policy_violation"] = not has_no_ff or has_ff_only
            result["kind"] = "git-write"
            return result
        if normalized in _GIT_READONLY_SUBCOMMANDS:
            readonly_args = args[arg_index + 1 :]
            _validate_git_readonly_context(
                normalized,
                readonly_args,
                config_overrides=config_overrides,
                env_overrides=env_overrides,
            )
            if normalized == "branch":
                _validate_git_branch_readonly_arguments(readonly_args)
            return {
                "kind": "git-readonly",
                "git_subcommand": normalized,
            }
        if normalized in _GIT_NON_WRITING_SUBCOMMANDS or normalized in _GIT_TERMINAL_GLOBAL_OPTIONS:
            return {
                "kind": "git-readonly",
                "git_subcommand": "help" if normalized.startswith("-") else normalized,
            }
        if token == "-C":
            if arg_index + 1 >= len(args):
                raise HookPayloadError("git command is missing a value for -C")
            current_dir, current_dir_known = _apply_git_change_directory(
                current_dir,
                current_dir_known,
                args[arg_index + 1],
            )
            arg_index += 2
            continue
        if token.startswith("-C") and token != "-C":
            current_dir, current_dir_known = _apply_git_change_directory(
                current_dir,
                current_dir_known,
                token[2:],
            )
            arg_index += 1
            continue
        if token == "-c":
            if arg_index + 1 >= len(args):
                raise HookPayloadError("git command is missing a value for -c")
            config_overrides = True
            arg_index += 2
            continue
        if token.startswith("-c") and token != "-c":
            config_overrides = True
            arg_index += 1
            continue
        if normalized in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if arg_index + 1 >= len(args):
                raise HookPayloadError(f"git command is missing a value for {token}")
            option_value = args[arg_index + 1]
            if normalized == "--git-dir":
                git_dir = _resolve_existing_path(
                    option_value,
                    current_dir if current_dir_known else None,
                    require_directory=False,
                )
                git_dir_error = git_dir is None
            elif normalized == "--work-tree":
                work_tree = _resolve_existing_path(
                    option_value,
                    current_dir if current_dir_known else None,
                    require_directory=True,
                )
                work_tree_error = work_tree is None
            arg_index += 2
            continue
        if normalized.startswith("--git-dir="):
            git_dir = _resolve_existing_path(
                token.split("=", 1)[1],
                current_dir if current_dir_known else None,
                require_directory=False,
            )
            git_dir_error = git_dir is None
            arg_index += 1
            continue
        if normalized.startswith("--work-tree="):
            work_tree = _resolve_existing_path(
                token.split("=", 1)[1],
                current_dir if current_dir_known else None,
                require_directory=True,
            )
            work_tree_error = work_tree is None
            arg_index += 1
            continue
        if any(normalized.startswith(prefix) for prefix in _GIT_INLINE_VALUE_PREFIXES):
            arg_index += 1
            continue
        if normalized in _GIT_GLOBAL_OPTIONS_NO_VALUE:
            arg_index += 1
            continue
        if token == "--" or token.startswith("-"):
            raise HookPayloadError("git command could not be classified safely")
        raise HookPayloadError(f"git subcommand '{token}' is not allowed by Claude pretool policy")
    return {
        "kind": "git-readonly",
        "git_subcommand": "help",
    }


def _classify_git_command_segment(
    tokens: list[str],
    shell_type: str,
    shell_state: dict[str, Any],
    *,
    nested_depth: int,
) -> dict[str, str] | None:
    if not tokens:
        return None
    normalized_shell = _normalize_text(shell_type).lower()
    current_dir = shell_state.get("current_dir")
    current_dir_known = bool(shell_state.get("current_dir_known"))
    env_overrides = dict(shell_state.get("git_env") or {})
    command_tokens = list(tokens)
    if normalized_shell == "bash":
        stripped = _strip_bash_safe_prefixes(command_tokens, current_dir, current_dir_known)
        if stripped is None:
            if _contains_git_executable_token(command_tokens):
                raise HookPayloadError("git command could not be classified safely")
            return None
        command_tokens, command_env, current_dir, current_dir_known = stripped
        env_overrides.update(command_env)
    elif normalized_shell == "powershell" and command_tokens[0] == "&":
        command_tokens = command_tokens[1:]
    if not command_tokens:
        return None
    if command_tokens[0].lower() in _GIT_CONTROL_PREFIXES and _contains_git_executable_token(command_tokens[1:]):
        raise HookPayloadError("git command behind shell control flow could not be classified safely")
    if _is_git_executable(command_tokens[0]):
        return _classify_direct_git_command(
            command_tokens,
            current_dir=current_dir,
            current_dir_known=current_dir_known,
            env_overrides=env_overrides,
        )
    nested_shell = _extract_nested_shell_wrapper_command(command_tokens)
    if nested_shell is not None:
        if nested_depth >= _MAX_NESTED_GIT_SHELL_DEPTH:
            raise HookPayloadError("nested shell Git command could not be classified safely")
        nested_shell_type, nested_command = nested_shell
        if not nested_command:
            raise HookPayloadError("nested shell Git command could not be classified safely")
        return _extract_git_command_context(
            nested_command,
            nested_shell_type,
            _clone_shell_state(current_dir, current_dir_known, env_overrides),
            nested_depth=nested_depth + 1,
        )
    if Path(command_tokens[0]).name.lower() in _GIT_SHELL_WRAPPERS and _contains_git_executable_token(command_tokens[1:]):
        raise HookPayloadError("nested shell Git command could not be classified safely")
    return None


def _extract_git_command_context(
    command: str,
    shell_type: str,
    shell_state: dict[str, Any],
    *,
    nested_depth: int = 0,
) -> dict[str, str] | None:
    if not command:
        return None
    git_results: list[dict[str, str]] = []
    normalized_shell = _normalize_text(shell_type).lower()
    for segment in _split_shell_command_segments(command, shell_type):
        tokens = _tokenize_shell_command_segment(segment, shell_type)
        if not tokens:
            continue
        if normalized_shell == "powershell":
            env_assignment = _parse_powershell_env_assignment_segment(tokens)
            if env_assignment is not None:
                name, value = env_assignment
                _store_git_env_override(shell_state["git_env"], name, value)
                continue
        if _apply_shell_change_directory_segment(tokens, shell_type, shell_state):
            continue
        parsed = _classify_git_command_segment(
            tokens,
            shell_type,
            shell_state,
            nested_depth=nested_depth,
        )
        if parsed is None:
            continue
        git_results.append(parsed)
    if not git_results:
        return None
    if any(result.get("kind") == "git-write" for result in git_results):
        if len(git_results) != 1:
            raise HookPayloadError("multiple Git commands in one compound shell command are not supported")
        return git_results[0]
    return {
        "kind": "git-readonly",
        "git_subcommand": git_results[0].get("git_subcommand", ""),
    }


def _extract_apply_patch_target_paths(tool_input: dict[str, Any]) -> list[str]:
    patch_text = ""
    for key in ("patch", "input", "content"):
        patch_text = _normalize_text(tool_input.get(key))
        if patch_text:
            break
    if not patch_text:
        patch_text = _extract_bash_command({}, tool_input)

    targets: list[str] = []
    prefixes = ("*** Add File:", "*** Update File:", "*** Delete File:")
    for line in patch_text.splitlines():
        for prefix in prefixes:
            if line.startswith(prefix):
                target = line[len(prefix) :].strip()
                if target and target not in targets:
                    targets.append(target)
                break
    return targets


def _is_path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def classify_agent_pretool_payload(
    hook_payload: dict[str, Any],
    cwd: str,
    provider: str,
) -> dict[str, Any]:
    normalized_provider = _normalize_text(provider).lower()
    if normalized_provider not in _AGENT_HOOK_PROVIDER_LABELS:
        raise HookPayloadError(f"unsupported agent policy provider '{normalized_provider or provider}'")

    tool_name = _extract_tool_name(hook_payload)
    reported_tool_name = tool_name.lower()
    normalized_tool_name = _TOOL_NAME_ALIASES.get(reported_tool_name, reported_tool_name)
    tool_input = _extract_tool_input(hook_payload)
    normalized_cwd = _normalize_text(cwd)
    hook_context: dict[str, Any] = {
        "tool_name": tool_name,
        "cwd": normalized_cwd,
    }

    if normalized_tool_name == "apply_patch":
        target_paths = _extract_apply_patch_target_paths(tool_input)
        if not target_paths:
            return {
                "classified": False,
                "reason": "apply_patch input does not specify a supported file header",
                "hook": hook_context,
            }
        repo_root_text = _find_git_worktree_root(normalized_cwd)
        if not repo_root_text:
            return {
                "classified": False,
                "reason": "hook cwd is not inside a git worktree",
                "hook": hook_context,
            }
        repo_root_path = Path(repo_root_text).resolve()
        resolved_targets: list[dict[str, str]] = []
        excluded_targets: list[str] = []
        for target_path in target_paths:
            resolved_path = _resolve_tool_target_path(target_path, normalized_cwd)
            if not _is_path_within(resolved_path, repo_root_path):
                excluded_targets.append(target_path)
                continue
            nearest_dir = _nearest_existing_directory(resolved_path)
            resolved_targets.append(
                {
                    "input_path": target_path,
                    "resolved_path": str(resolved_path),
                    "action_cwd": str(nearest_dir or resolved_path.parent),
                }
            )
        hook_context["target_paths"] = [item["input_path"] for item in resolved_targets]
        if excluded_targets:
            hook_context["excluded_target_paths"] = excluded_targets
        if not resolved_targets:
            return {
                "classified": False,
                "reason": "apply_patch targets are outside the current git worktree",
                "hook": hook_context,
            }
        repo_root = str(repo_root_path)
        normalized_cwd = resolved_targets[0]["action_cwd"]
        hook_context["target_path"] = resolved_targets[0]["input_path"]
        hook_context["resolved_target_path"] = resolved_targets[0]["resolved_path"]
        hook_context["git_worktree_root"] = repo_root
        tags = ["codex-pretool", "apply_patch"]
    elif normalized_tool_name in _WRITE_TOOL_NAMES:
        target_paths = _extract_write_target_paths(normalized_tool_name, tool_input)
        if not target_paths:
            return {
                "classified": False,
                "reason": "tool input does not specify a target path",
                "hook": hook_context,
            }
        resolved_targets: list[dict[str, str]] = []
        for target_path in target_paths:
            resolved_path = _resolve_tool_target_path(target_path, normalized_cwd)
            repo_root = _find_git_worktree_root(str(resolved_path))
            nearest_dir = _nearest_existing_directory(resolved_path)
            resolved_targets.append(
                {
                    "input_path": target_path,
                    "resolved_path": str(resolved_path),
                    "action_cwd": str(nearest_dir or resolved_path.parent),
                    "git_worktree_root": repo_root or "",
                }
            )
        hook_context["target_paths"] = [item["input_path"] for item in resolved_targets]
        repo_target = next((item for item in resolved_targets if item["git_worktree_root"]), None)
        if repo_target is None:
            return {
                "classified": False,
                "reason": "target path is not inside a git worktree",
                "hook": hook_context,
            }
        repo_root = repo_target["git_worktree_root"]
        normalized_cwd = repo_target["action_cwd"]
        hook_context["target_path"] = repo_target["input_path"]
        hook_context["resolved_target_path"] = repo_target["resolved_path"]
        hook_context["git_worktree_root"] = repo_root
        tags = [f"{normalized_provider.replace('-code', '')}-pretool", normalized_tool_name]
    elif normalized_tool_name in _COMMAND_TOOL_NAMES:
        command = _extract_bash_command(hook_payload, tool_input)
        hook_context["command"] = command
        parsed_command = _extract_git_command_context(
            command,
            normalized_tool_name,
            _initialize_shell_state(normalized_cwd),
        )
        if parsed_command is None:
            return {
                "classified": False,
                "reason": f"{normalized_tool_name} command is out of scope for repo-write enforcement",
                "hook": hook_context,
            }
        if parsed_command.get("kind") == "git-readonly":
            hook_context["git_subcommand"] = parsed_command.get("git_subcommand", "")
            return {
                "classified": False,
                "reason": "git command is a safe read-only operation",
                "hook": hook_context,
            }
        repo_root = parsed_command["git_worktree_root"]
        normalized_cwd = parsed_command["request_cwd"]
        hook_context["git_command_cwd"] = parsed_command["git_command_cwd"]
        hook_context["git_worktree_root"] = repo_root
        hook_context["git_write_subcommand"] = parsed_command["git_write_subcommand"]
        if parsed_command["git_write_subcommand"] == "merge":
            hook_context["merge_has_no_ff"] = bool(parsed_command.get("merge_has_no_ff"))
            hook_context["merge_has_ff_only"] = bool(parsed_command.get("merge_has_ff_only"))
            hook_context["merge_policy_violation"] = bool(parsed_command.get("merge_policy_violation"))
        tags = [
            f"{normalized_provider.replace('-code', '')}-pretool",
            normalized_tool_name,
            f"git-{parsed_command['git_write_subcommand']}",
        ]
    else:
        return {
            "classified": False,
            "reason": "tool is read-only or out of scope",
            "hook": hook_context,
        }

    project_key = resolve_project_key(cwd=repo_root)
    return {
        "classified": True,
        "reason": "",
        "hook": hook_context,
        "request": {
            "caller": normalized_provider,
            "action": f"{normalized_provider.replace('-code', '')}-pretool",
            "scope_key": resolve_scope_key(cwd=repo_root, project_key=project_key),
            "project_key": project_key,
            "cwd": normalized_cwd,
            "action_metadata": {
                "mode": "repo-write",
                "category": f"{normalized_provider.replace('-code', '')}-pretool",
                "tags": [*tags, "policy-guidance"],
            },
            "execute_guards": True,
            "persist_audit": True,
            "advisory_only": True,
        },
    }


def classify_claude_pretool_payload(hook_payload: dict[str, Any], cwd: str) -> dict[str, Any]:
    """Compatibility alias for the original Claude-specific classifier."""
    return classify_agent_pretool_payload(hook_payload, cwd, "claude-code")


def _decision_reason(result: dict[str, Any]) -> str:
    decision = _normalize_text(
        result.get("policy_decision")
        if result.get("advisory_only")
        else result.get("decision")
    ).lower()
    executed = result.get("executed_guard_results")
    if isinstance(executed, list):
        if decision == "error":
            for item in executed:
                if isinstance(item, dict) and _normalize_text(item.get("status")) == "error":
                    reason = _normalize_text(item.get("reason"))
                    if reason:
                        return reason
        if decision == "blocked":
            for status in ("fail", "error"):
                for item in executed:
                    if isinstance(item, dict) and _normalize_text(item.get("status")) == status:
                        reason = _normalize_text(item.get("reason"))
                        if reason:
                            return reason
    if decision == "blocked":
        blocking = result.get("blocking_guards")
        if isinstance(blocking, list):
            for item in blocking:
                if isinstance(item, dict):
                    reason = _normalize_text(item.get("content"))
                    if reason:
                        return reason
        return "policy blocked action"
    if decision == "workflow_required":
        workflows = result.get("required_workflows")
        if isinstance(workflows, list):
            for item in workflows:
                if isinstance(item, dict):
                    reason = _normalize_text(item.get("content")) or _normalize_text(item.get("workflow_skill_id"))
                    if reason:
                        return reason
        return "policy requires a workflow before this action"
    if decision == "error":
        return _normalize_text(result.get("reason")) or "policy preflight engine error"
    return ""


def _exit_code_for_decision(decision: str) -> int:
    normalized = _normalize_text(decision).lower()
    if normalized == "error":
        return ERROR_EXIT_CODE
    if normalized == "blocked":
        return BLOCKED_EXIT_CODE
    return ALLOW_EXIT_CODE


def _run_preflight_request(
    request_payload: dict[str, Any],
    *,
    request_type: str,
    extras: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int, str]:
    try:
        result = preflight_directives(
            caller=_normalize_text(request_payload.get("caller")) or "all",
            user_query=str(request_payload.get("user_query") or ""),
            action=str(request_payload.get("action") or ""),
            scope_key=str(request_payload.get("scope_key") or ""),
            project_key=str(request_payload.get("project_key") or ""),
            persist_audit=_coerce_bool(request_payload.get("persist_audit"), True),
            cwd=str(request_payload.get("cwd") or ""),
            action_metadata=_coerce_mapping(request_payload.get("action_metadata")),
            chore_intent=_coerce_mapping(request_payload.get("chore_intent")),
            independent_task_context=_coerce_mapping(request_payload.get("independent_task_context")),
            execute_guards=_coerce_bool(request_payload.get("execute_guards"), False),
            advisory_only=_coerce_bool(request_payload.get("advisory_only"), False),
        )
    except Exception as exc:
        reason = _normalize_text(exc) or "policy preflight engine error"
        return _error_output(reason, request_type=request_type, extras=extras)

    if not isinstance(result, dict):
        return _error_output("policy preflight returned an invalid response", request_type=request_type, extras=extras)

    output = dict(result)
    reason = _decision_reason(output)
    exit_code = _exit_code_for_decision(str(output.get("decision") or "error"))
    output["exit_code"] = exit_code
    output["reason"] = reason
    output["request_type"] = request_type
    if extras:
        output.update(extras)
    stderr_reason = reason if exit_code != ALLOW_EXIT_CODE else ""
    return output, exit_code, stderr_reason


def _parse_json_object(raw_text: str, *, request_type: str) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int, str] | None]:
    text = raw_text.strip()
    if not text:
        return None, _error_output("request JSON is required", request_type=request_type)
    try:
        payload = json.loads(text)
    except Exception:
        return None, _error_output("malformed request JSON", request_type=request_type)
    if not isinstance(payload, dict):
        return None, _error_output("request JSON must be an object", request_type=request_type)
    return payload, None


def _load_hook_payload(payload: dict[str, Any], *, provider_label: str = "Claude") -> dict[str, Any]:
    hook_payload = payload.get("hook_payload", payload.get("hook"))
    if isinstance(hook_payload, dict):
        return dict(hook_payload)
    if isinstance(hook_payload, str):
        text = hook_payload.strip()
        if not text:
            raise HookPayloadError(f"{provider_label} hook payload JSON is required")
        try:
            parsed = json.loads(text)
        except Exception:
            raise HookPayloadError(f"malformed {provider_label} hook payload JSON")
        if not isinstance(parsed, dict):
            raise HookPayloadError(f"{provider_label} hook payload JSON must be an object")
        return dict(parsed)
    raise HookPayloadError(f"{provider_label} hook payload JSON is required")


def process_policy_request(payload: dict[str, Any]) -> tuple[dict[str, Any], int, str]:
    request_type = _normalize_text(payload.get("request_type") or "preflight").lower() or "preflight"
    caller = _normalize_text(payload.get("caller")).lower()
    is_guidance_hook = request_type in _AGENT_HOOK_REQUEST_TYPES or caller == "git-hook"
    guidance_level = normalize_policy_guidance_level(
        get_cfg_value("directives.policy.guidance_level", "warn")
    )

    def with_guidance_level(
        response: tuple[dict[str, Any], int, str],
    ) -> tuple[dict[str, Any], int, str]:
        if not is_guidance_hook:
            return response
        output, exit_code, stderr_reason = response
        enriched = dict(output)
        enriched["guidance_level"] = guidance_level
        enriched["guidance_enabled"] = guidance_level != "off"
        return enriched, exit_code, stderr_reason

    if is_guidance_hook and guidance_level == "off":
        return (
            {
                "decision": "allow",
                "exit_code": ALLOW_EXIT_CODE,
                "reason": "policy guidance is disabled",
                "request_type": request_type,
                "classified": False,
                "guidance_enabled": False,
                "guidance_level": "off",
            },
            ALLOW_EXIT_CODE,
            "",
        )
    if request_type not in _AGENT_HOOK_REQUEST_TYPES:
        return with_guidance_level(_run_preflight_request(payload, request_type="preflight"))

    provider = _AGENT_HOOK_REQUEST_TYPES[request_type]

    try:
        hook_payload = _load_hook_payload(
            payload,
            provider_label=_AGENT_HOOK_PROVIDER_LABELS.get(provider, provider),
        )
        classified = classify_agent_pretool_payload(
            hook_payload,
            str(payload.get("cwd") or ""),
            provider,
        )
    except HookPayloadError as exc:
        return with_guidance_level(_error_output(
            _normalize_text(exc) or f"invalid {provider} hook payload",
            request_type=request_type,
        ))

    hook_context = classified.get("hook") if isinstance(classified.get("hook"), dict) else {}
    if not classified.get("classified"):
        return with_guidance_level((
            {
                "decision": "allow",
                "exit_code": ALLOW_EXIT_CODE,
                "reason": str(classified.get("reason") or ""),
                "request_type": request_type,
                "classified": False,
                "hook": hook_context,
            },
            ALLOW_EXIT_CODE,
            "",
        ))

    request = classified.get("request")
    if not isinstance(request, dict):
        return with_guidance_level(_error_output(
            f"classified {provider} hook request was invalid",
            request_type=request_type,
            extras={"classified": True, "hook": hook_context},
        ))

    if hook_context.get("merge_policy_violation"):
        reason = "Agent Git merges must use --no-ff and must not use --ff-only."
        return with_guidance_level((
            {
                "decision": "advisory",
                "policy_decision": "blocked",
                "final_status": "advisory",
                "advisory_only": True,
                "would_block": True,
                "exit_code": ALLOW_EXIT_CODE,
                "reason": reason,
                "request_type": request_type,
                "classified": True,
                "hook": hook_context,
            },
            ALLOW_EXIT_CODE,
            "",
        ))

    return with_guidance_level(_run_preflight_request(
        request,
        request_type=request_type,
        extras={"classified": True, "hook": hook_context},
    ))


def process_policy_request_text(raw_text: str) -> tuple[dict[str, Any], int, str]:
    payload, error = _parse_json_object(raw_text, request_type="preflight")
    if error is not None:
        return error
    return process_policy_request(payload or {})


def _read_request_text(request_file: str) -> tuple[str | None, tuple[dict[str, Any], int, str] | None]:
    request_path = _normalize_text(request_file)
    if request_path:
        try:
            return Path(request_path).read_text(encoding="utf-8"), None
        except OSError:
            return None, _error_output("request file could not be read", request_type="preflight")

    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return None, _error_output("request JSON is required", request_type="preflight")
    try:
        return stdin.read(), None
    except Exception:
        return None, _error_output("request JSON could not be read", request_type="preflight")


def _emit_policy_output(
    output: dict[str, Any],
    *,
    stderr_reason: str,
    response_file: str,
    error_file: str,
) -> None:
    response_text = _compact_json(output)
    response_path = _normalize_text(response_file)
    error_path = _normalize_text(error_file)
    if response_path:
        _write_text_atomic(response_path, response_text)
    else:
        print(response_text)
    if error_path:
        _write_text_atomic(error_path, stderr_reason)
    elif stderr_reason:
        print(stderr_reason, file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    _reconfigure_std_streams()

    parser = argparse.ArgumentParser()
    parser.add_argument("--request-file", default="")
    parser.add_argument("--response-file", default="")
    parser.add_argument("--error-file", default="")
    args = parser.parse_args(argv)

    raw_text, read_error = _read_request_text(args.request_file)
    if read_error is not None:
        output, exit_code, stderr_reason = read_error
    else:
        output, exit_code, stderr_reason = process_policy_request_text(raw_text or "")

    try:
        _emit_policy_output(
            output,
            stderr_reason=stderr_reason,
            response_file=args.response_file,
            error_file=args.error_file,
        )
    except OSError:
        fallback_reason = "policy response could not be written"
        error_path = _normalize_text(args.error_file)
        if error_path:
            try:
                _write_text_atomic(error_path, fallback_reason)
            except OSError:
                pass
        elif not _normalize_text(args.response_file):
            print(fallback_reason, file=sys.stderr)
        raise SystemExit(ERROR_EXIT_CODE)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
