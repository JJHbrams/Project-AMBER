"""Common Claude lifecycle hook provisioning. Dry-run unless explicitly applied.

Requires prompt_id (introduced in 2.1.196) AND native mcp_tool support.
The intended validation target is 2.1.263; 2.1.196 alone is not sufficient.
No commands, raw hook bodies,
transcript paths, prompts, tool inputs or provider resume IDs are forwarded.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import re
import tempfile
import shutil
import subprocess
import uuid

TOOL = "engram_report_claude_event"
EVENTS = (
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Stop",
    "StopFailure",
    "SessionEnd",
)
TOOL_EVENTS = frozenset(
    {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"}
)

# Static metadata reminder: no stdin, transcript, bootstrap or application call.
# Keep monitoring independent of session.auto_inject (memory bootstrap opt-out).
LEGACY_TITLE_REMINDERS = ((
    "Engram session monitor: on this new or resumed connection, report a safe 2-8 word "
    "task title with the available engram_report_session_title tool before answering "
    "the first substantive request, even if an earlier connection already received "
    "a title. Reuse the same safe title when the task is unchanged. Discover the tool "
    "only if needed. Do not include transcript text, paths, tool inputs or secrets. "
    "Do not call lifecycle state reporting tools yourself."
),)
TITLE_REMINDER = (
    "Engram session monitor: register a title for the connection used by this "
    "invocation, including when resuming an existing conversation. A title "
    "reported on an earlier connection is not registered on this connection. "
    "Before answering the current user request, call engram_report_session_title "
    "again with a safe 2-8 word public task label. Reuse the prior safe title when "
    "the task is unchanged. Discover the tool only if needed. Do not include "
    "transcript text, paths, tool inputs or secrets. Do not repeat memory "
    "bootstrap or call lifecycle state reporting tools yourself."
)


def title_reminder_command(reminder=TITLE_REMINDER, *, windows=None):
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return ('powershell.exe -NoProfile -NonInteractive -Command "Write-Output '
                "'" + reminder + "'; # engram-monitor-connection-title\"")
    return ('/bin/sh -c "printf \'%s\\n\' \'' + reminder
            + "'; # engram-monitor-connection-title\"")


def owned_title_reminder_commands():
    """Only exact shipped commands, never marker-substring ownership."""
    return {title_reminder_command(text, windows=windows)
            for text in (*LEGACY_TITLE_REMINDERS, TITLE_REMINDER)
            for windows in (True, False)}


RESTORE_TITLE_REMINDER = ('Engram session monitor: native hooks restore the previous safe title for this session. '
    'Do not regenerate a title just because the connection resumed. Report a safe 2-8 word public task title '
    'with engram_report_session_title only when a native hook requests a missing title or the task materially changes. '
    'Never include paths, transcripts, tool inputs or secrets. Native hooks alone report lifecycle state.')


def generated_hooks(server="engram", *, native_identity=False, subagent_activity=False):
    native_identity = native_identity or subagent_activity
    if not isinstance(server, str) or not re.fullmatch(
        r"[A-Za-z0-9_.:-]{1,80}", server
    ):
        raise ValueError("invalid MCP server name")
    result = {}
    for event in EVENTS + (('SubagentStart', 'SubagentStop') if subagent_activity else ()):
        arguments = {"event": event, "agent_id": "${agent_id}"}
        if native_identity:
            arguments['native_session_id'] = '${session_id}'
        if event != 'SessionEnd':
            arguments["turn_id"] = "${prompt_id}"
        if event in TOOL_EVENTS:
            arguments["tool_name"] = "${tool_name}"
            if event != "PermissionRequest":
                arguments["tool_use_id"] = "${tool_use_id}"
        handler = {
            "type": "mcp_tool",
            "server": server,
            "tool": TOOL,
            "input": arguments,
            "timeout": 2,
        }
        group = {"hooks": [handler]}
        if event in TOOL_EVENTS:
            group["matcher"] = (
                r"^(?!mcp__.*__engram_report_claude_event$)(?!engram_report_claude_event$).*"
            )
        result[event] = [group]
    result["SessionStart"] = [{"matcher": "startup|resume|clear|compact", "hooks": [
        {"type": "command", "command": title_reminder_command(RESTORE_TITLE_REMINDER if native_identity else TITLE_REMINDER), "timeout": 5}
    ]}]
    return result


def merge_settings(settings, server="engram", *, upgrade_native_identity=False, upgrade_subagent_activity=False):
    """Replace only this server's Engram-owned native handlers, never Orca hooks."""
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    result = copy.deepcopy(settings)
    if result.get("disableAllHooks") is True:
        return result
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("invalid hooks object")
    generated = generated_hooks(server, native_identity=upgrade_native_identity, subagent_activity=upgrade_subagent_activity)
    newest = generated_hooks(server, subagent_activity=True)
    supported = (generated_hooks(server), generated_hooks(server, native_identity=True), newest)
    installed = set()
    owned_commands = owned_title_reminder_commands()
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            raise ValueError("invalid hook groups")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("invalid hook group")
            # Other Engram installers also append their own hook groups. Keep
            # one exact current group where it already is instead of competing
            # over ordering and generating a settings backup on every rebuild.
            if any(group in version.get(event, []) for version in supported):
                if event not in installed:
                    replace = upgrade_subagent_activity or (upgrade_native_identity and group not in newest.get(event, []))
                    kept.append(generated[event][0] if replace else group)
                    installed.add(event)
                continue
            if any(isinstance(h, dict) and h.get('type') == 'mcp_tool'
                   and h.get('server') == server and h.get('tool') == TOOL for h in group['hooks']):
                raise ValueError('modified native monitor hook preserved')
            handlers = group["hooks"]
            remaining = [
                handler
                for handler in handlers
                if not (
                    isinstance(handler, dict)
                    and ((handler.get("type") == "mcp_tool"
                          and handler.get("server") == server
                          and handler.get("tool") == TOOL)
                         or (event == "SessionStart"
                             and handler.get("type") == "command"
                             and isinstance(handler.get("command"), str)
                             and handler["command"] in owned_commands))
                )
            ]
            if remaining or not handlers:
                kept.append({**group, "hooks": remaining})
        hooks[event] = kept
    for event, groups in generated.items():
        if event not in installed:
            hooks.setdefault(event, []).extend(groups)
    return result


def configure(path, *, server="engram", apply=False, upgrade_native_identity=False, upgrade_subagent_activity=False):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("symlink settings are unsupported")
    if path.exists() and path.stat().st_size > 2_000_000:
        raise ValueError("settings too large")
    original = path.read_bytes() if path.exists() else None
    if original is not None and len(original) > 2_000_000:
        raise ValueError("settings changed during read")
    settings = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    merged = merge_settings(settings, server, upgrade_native_identity=upgrade_native_identity, upgrade_subagent_activity=upgrade_subagent_activity)
    changed = merged != settings
    if apply and changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve every pre-change revision; never overwrite an earlier backup.
        backup = path.with_name(path.name + ".engram-monitor-backup")
        if original is not None:
            if backup.exists():
                backup = path.with_name(path.name + ".engram-monitor-backup-" + uuid.uuid4().hex)
            with backup.open("xb") as stream:
                stream.write(original)
        fd, temporary = tempfile.mkstemp(prefix=".engram-hooks-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(merged, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if (path.read_bytes() if path.exists() else None) != original:
                raise ValueError("settings changed during apply")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    # Never print the user's other settings, commands or credentials.
    return {
        "mode": "apply" if apply else "dry-run",
        "changed": changed,
        "managed_hooks": generated_hooks(server, native_identity=upgrade_native_identity,
                                         subagent_activity=upgrade_subagent_activity),
        "hook_count": len(EVENTS) + 1 + sum(
            generated_hooks(server, subagent_activity=True)[event][0] in merged.get('hooks', {}).get(event, [])
            for event in ('SubagentStart', 'SubagentStop')),
        "limitations": [
            "Already-connected MCP server required.",
            "Requires native mcp_tool support; intended validation target 2.1.263.",
            "Every changed apply preserves a recoverable settings backup.",
            "Unresolved agent_id placeholders fail closed.",
            "Stop is an attempt; another hook may resume work.",
            "Quiet working expires after 120 seconds.",
        ],
    }



VALIDATED_MINIMUM = (2, 1, 263)


def default_settings_path():
    directory = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(directory) if directory else Path.home() / ".claude") / "settings.json"


def compatibility():
    """Probe only CLI version, never create a Claude conversation."""
    executable = shutil.which("claude")
    if not executable:
        return False, "claude-not-installed"
    try:
        completed = subprocess.run(
            [executable, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "claude-version-unavailable"
    version = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\s|$)", completed.stdout.strip())
    if completed.returncode or not version:
        return False, "claude-version-unavailable"
    if tuple(map(int, version.groups())) < VALIDATED_MINIMUM:
        return False, "claude-below-validated-minimum"
    return True, "version-compatible-native-runtime-verification-required"


def provision(path=None, *, server="engram", apply=False, upgrade_native_identity=False, upgrade_subagent_activity=False):
    """Installer-facing sanitized result. Respect explicit user hook disable."""
    supported, reason = compatibility()
    result = {"supported": supported, "changed": False, "applied": False,
              "hook_count": 0, "reason": reason}
    if not supported:
        return result
    path = Path(path) if path is not None else default_settings_path()
    if path.is_symlink():
        raise ValueError("symlink settings are unsupported")
    if path.exists():
        if path.stat().st_size > 2_000_000:
            raise ValueError("settings too large")
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError("settings must be an object")
        if settings.get("disableAllHooks") is True:
            return {**result, "reason": "user-disabled-all-hooks"}
    configured = configure(path, server=server, apply=apply, upgrade_native_identity=upgrade_native_identity, upgrade_subagent_activity=upgrade_subagent_activity)
    return {**result, "changed": configured["changed"],
            "applied": bool(apply), "hook_count": configured['hook_count']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=None)
    parser.add_argument("--provision", action="store_true")
    parser.add_argument("--server", default="engram")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument('--upgrade-native-identity', action='store_true', help='Explicit one-time hook identity upgrade; review changed hooks in the provider.')
    parser.add_argument('--upgrade-subagent-activity', action='store_true', help='Explicit child lifecycle upgrade; implies native identity.')
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                (provision(args.settings, server=args.server, apply=args.apply, upgrade_native_identity=args.upgrade_native_identity, upgrade_subagent_activity=args.upgrade_subagent_activity)
                 if args.provision else configure(args.settings or default_settings_path(), server=args.server, apply=args.apply, upgrade_native_identity=args.upgrade_native_identity, upgrade_subagent_activity=args.upgrade_subagent_activity)),
                ensure_ascii=False,
                indent=None if args.provision else 2,
            )
        )
    except (OSError, ValueError) as error:
        raise SystemExit("Hook configuration failed: " + type(error).__name__) from None


if __name__ == "__main__":
    main()
