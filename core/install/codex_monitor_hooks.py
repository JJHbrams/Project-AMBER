"""Codex-owned native monitoring hooks; never approves trust or reads transcripts."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from .codex_hook_status import inspect_root


class HookConflict(ValueError):
    """A user-modified monitor definition cannot be safely adopted."""

TOOL = 'engram_report_codex_event'
EVENTS = ('UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PermissionRequest', 'Stop', 'Interrupt',
          'SubagentStart', 'SubagentStop')
SUBAGENT_EVENTS = ('SubagentStart', 'SubagentStop')
TITLE_REMINDER = (
    'Engram Codex session monitor: before answering the first substantive request '
    'on this new or resumed connection, call engram_report_session_title with a '
    'safe 2-8 word public task label, even if an earlier connection had a title. '
    'Reuse the prior safe title when appropriate. Discover the tool if needed. '
    'Never include transcript text, paths, tool inputs or secrets. Do not call '
    'state or lifecycle reporters yourself; native hooks report lifecycle.'
)


RESTORE_TITLE_REMINDER = ('Engram session monitor: native hooks restore the previous safe title for this session. '
    'Do not regenerate a title just because the connection resumed. Report a safe 2-8 word public task title '
    'with engram_report_session_title only when a native hook requests a missing title or the task materially changes. '
    'Never include paths, transcripts, tool inputs or secrets. Native hooks alone report lifecycle state.')


def title_command(*, windows=None, native_identity=False):
    reminder = RESTORE_TITLE_REMINDER if native_identity else TITLE_REMINDER
    if windows is None:
        windows = os.name == 'nt'
    if windows:
        return ('powershell.exe -NoProfile -NonInteractive -Command "Write-Output '
                "'" + reminder + "'\"")
    return "/bin/sh -c \"printf '%s\\n' '" + reminder + "'\""


def supported_title_commands():
    return {title_command(windows=windows, native_identity=identity)
            for windows in (True, False) for identity in (True, False)}


def generated_hooks(server='engram', *, native_identity=False, subagent_activity=True):
    """Return the canonical eight monitored events plus SessionStart.

    Definition installation is intentionally distinct from Codex trust review.
    """
    native_identity = native_identity or subagent_activity
    if not isinstance(server, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', server):
        raise ValueError('invalid server')
    result = {}
    for event in EVENTS if subagent_activity else tuple(event for event in EVENTS if event not in SUBAGENT_EVENTS):
        payload = {'event': event, 'turn_id': '${turn_id}'}
        if event in SUBAGENT_EVENTS:
            payload['agent_id'] = '${agent_id}'
        if native_identity:
            payload['native_session_id'] = '${session_id}'
        if event in ('PreToolUse', 'PostToolUse', 'PermissionRequest'):
            payload['tool_name'] = '${tool_name}'
        if event in ('PreToolUse', 'PostToolUse'):
            payload['tool_use_id'] = '${tool_use_id}'
        # Codex MCP hooks do not recursively invoke hooks. No unsupported Rust
        # regex lookahead or undocumented agent_id interpolation is necessary.
        result[event] = [{'hooks': [{'type': 'mcp_tool', 'server': server,
            'tool': TOOL, 'input': payload, 'timeout': 2}]}]
    result['SessionStart'] = [{'hooks': [{'type': 'command',
        'command': title_command(native_identity=native_identity), 'timeout': 5}]}]
    return result


def merge_settings(settings, server='engram', *, upgrade_native_identity=False, upgrade_subagent_activity=False):
    if not isinstance(settings, dict):
        raise ValueError('hooks document must be an object')
    result = copy.deepcopy(settings)
    hooks = result.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('hooks must be an object')
    generated = generated_hooks(server, native_identity=upgrade_native_identity, subagent_activity=upgrade_subagent_activity)
    newest = generated_hooks(server, subagent_activity=True)
    supported = (generated_hooks(server, subagent_activity=False), generated_hooks(server, native_identity=True, subagent_activity=False), newest)
    owned_commands = supported_title_commands()
    present = set()
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError('hook groups must be arrays')
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                raise ValueError('invalid hook group')
            if any(group in version.get(event, []) for version in supported):
                if event not in present:
                    replace = upgrade_subagent_activity or (upgrade_native_identity and group not in newest.get(event, []))
                    kept.append(generated[event][0] if replace else group)
                    present.add(event)
                continue
            remaining = []
            for handler in group['hooks']:
                if not isinstance(handler, dict):
                    raise ValueError('invalid hook handler')
                owned = (handler.get('type') == 'mcp_tool' and
                         handler.get('server') == server and handler.get('tool') == TOOL)
                owned |= (event == 'SessionStart' and handler.get('type') == 'command'
                          and isinstance(handler.get('command'), str)
                          and handler.get('command') in owned_commands)
                if owned:
                    # Matching a tool name is not ownership: matcher, timeout,
                    # input or mixed-group edits change native trust hashes.
                    raise HookConflict('modified-monitor-definition-preserved')
                remaining.append(handler)
            if remaining or not group['hooks']:
                kept.append({**group, 'hooks': remaining})
        hooks[event] = kept
    for event, groups in generated.items():
        if event not in present:
            hooks.setdefault(event, []).extend(groups)
    return result


def _read(path, *, binary=False):
    if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
        raise ValueError('linked configuration unsupported')
    if not path.exists():
        return None
    if path.stat().st_size > 2_000_000:
        raise ValueError('configuration too large')
    value = path.read_bytes()
    if len(value) > 2_000_000:
        raise ValueError('configuration too large')
    return value if binary else value.decode('utf-8-sig')


def existing_roots(*, home=None, environ=None):
    env = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    candidates = [home / '.codex']
    if env.get('CODEX_HOME'):
        candidates.append(Path(env['CODEX_HOME']))
    if env.get('APPDATA'):
        candidates.append(Path(env['APPDATA']) / 'orca/codex-runtime-home/home')
    roots, seen = [], set()
    for root in candidates:
        key = os.path.normcase(str(root.absolute()))
        if key not in seen and root.is_dir() and (root / 'config.toml').is_file():
            seen.add(key)
            roots.append(root)
    return roots


def configure_root(root, *, server='engram', apply=False, inspect=True, upgrade_native_identity=False, upgrade_subagent_activity=False):
    root = Path(root).absolute()
    if root.is_symlink() or getattr(root, 'is_junction', lambda: False)():
        raise ValueError('linked configuration root unsupported')
    config_path = root / 'config.toml'
    config_before = _read(config_path, binary=True)
    result = {'changed': False, 'applied': False, 'trust_required': False,
              'status': 'unknown', 'root': str(root), 'cwd': str(Path.cwd()),
              'review_instruction': 'Open Codex with CODEX_HOME=' + str(root)
                + ' and working directory ' + str(Path.cwd()) + '; enter /hooks to review this root.',
              'reason': 'configuration-root-missing'}
    if config_before is None:
        return result  # Never create unrelated provider roots.
    config = tomllib.loads(config_before.decode('utf-8-sig'))
    features = config.get('features', {})
    if features.get('hooks', features.get('codex_hooks', True)) is False:
        return {**result, 'reason': 'user-disabled-hooks', 'status': 'disabled'}
    for groups in config.get('hooks', {}).values():
        if isinstance(groups, list) and any(
            isinstance(group, dict) and any(isinstance(h, dict) and
                h.get('tool') == TOOL and h.get('server') == server
                for h in group.get('hooks', [])) for group in groups
        ):
            return {**result, 'reason': 'inline-monitor-hooks-preserved', 'conflict': True,
                    'status': 'conflict', 'review_instruction':
                    'Existing inline monitor hooks were preserved. Inspect the definitions in '
                    + str(config_path) + ' before changing them. ' + result['review_instruction']}
    path = root / 'hooks.json'
    original = _read(path, binary=True)
    document = json.loads(original.decode('utf-8-sig')) if original is not None else {}
    # A genuinely new Codex root receives the complete current contract.  An
    # existing hooks.json keeps its supported legacy shape unless the user
    # explicitly requests the trust-changing subagent upgrade.
    install_subagent_activity = upgrade_subagent_activity or original is None
    try:
        merged = merge_settings(document, server, upgrade_native_identity=upgrade_native_identity,
                                upgrade_subagent_activity=install_subagent_activity)
    except HookConflict:
        return {**result, 'reason': 'modified-monitor-definition-preserved', 'conflict': True,
                'status': 'conflict', 'review_instruction':
                'User-modified monitor hooks were preserved. Inspect the definitions in '
                + str(path) + ' before changing them. ' + result['review_instruction']}
    changed = merged != document
    if apply and changed:
        if original is not None:
            backup = path.with_name(path.name + '.engram-monitor-backup-' + uuid.uuid4().hex)
            with backup.open('xb') as stream:
                stream.write(original)
        fd, temporary = tempfile.mkstemp(prefix='.engram-codex-hooks-', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(merged, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            if _read(path, binary=True) != original or _read(config_path, binary=True) != config_before:
                raise ValueError('configuration changed during apply')
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    status = inspect_root(root, server=server) if inspect and (apply or not changed) else {
        'status': 'unknown', 'trust_required': False, 'reason': 'planned-hooks-not-inspected'}
    return {**result, **status, 'changed': changed, 'applied': bool(apply),
            'hook_count': len(EVENTS) - len(SUBAGENT_EVENTS) + 1 + sum(
                generated_hooks(server, subagent_activity=True)[event][0] in merged.get('hooks', {}).get(event, [])
                for event in SUBAGENT_EVENTS)}


def compatibility():
    executable = shutil.which('codex')
    if not executable:
        return False, 'codex-not-installed'
    try:
        run = subprocess.run([executable, '--version'], capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=5,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        return False, 'codex-version-unavailable'
    match = re.fullmatch(r'(?:codex-cli\s+)?(\d+)\.(\d+)\.(\d+)', run.stdout.strip())
    if run.returncode or not match:
        return False, 'codex-version-unavailable'
    if tuple(map(int, match.groups())) < (0, 153, 4):
        return False, 'codex-below-native-validation-target'
    return True, 'native-runtime-and-hook-review-required'


def provision(roots=None, *, server='engram', apply=False, upgrade_native_identity=False, upgrade_subagent_activity=False):
    supported, reason = compatibility()
    results = []
    if supported:
        selected = list(existing_roots() if roots is None else roots)
        # Reject malformed roots before changing any other provider root.
        # Each actual replacement still rechecks its source bytes for races.
        if apply:
            for root in selected:
                configure_root(root, server=server, apply=False, inspect=False, upgrade_native_identity=upgrade_native_identity, upgrade_subagent_activity=upgrade_subagent_activity)
        for root in selected:
            results.append(configure_root(root, server=server, apply=apply, upgrade_native_identity=upgrade_native_identity, upgrade_subagent_activity=upgrade_subagent_activity))
    return {'supported': supported, 'reason': reason, 'roots': results,
            'changed': any(r['changed'] for r in results),
            'applied': any(r['applied'] for r in results),
            'trust_required': any(r['trust_required'] for r in results),
            'trust_review_required': any(r['trust_required'] for r in results),
            'title_policy_review_required': bool(results),
            'title_policy_notice': 'Existing MCP tool approval policies are preserved; review engram_report_session_title access if title delivery is denied.',
            'root_count': len(results)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, action='append')
    parser.add_argument('--server', default='engram')
    parser.add_argument('--provision', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--upgrade-native-identity', action='store_true', help='Explicit one-time hook identity upgrade; changed hooks need native trust review.')
    parser.add_argument('--upgrade-subagent-activity', action='store_true', help='Explicit child lifecycle upgrade; implies native identity and requires hook review.')
    args = parser.parse_args(argv)
    try:
        result = provision(args.root, server=args.server, apply=args.apply, upgrade_native_identity=args.upgrade_native_identity, upgrade_subagent_activity=args.upgrade_subagent_activity)
        print(json.dumps(result))
        for root in result['roots']:
            if root['status'] != 'ready':
                print('Codex hooks: ' + root['status'] + ' (' + root['reason'] + '). '
                      + root['review_instruction'], file=sys.stderr)
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit('Codex hook configuration failed: ' + type(exc).__name__) from None


if __name__ == '__main__':
    main()
