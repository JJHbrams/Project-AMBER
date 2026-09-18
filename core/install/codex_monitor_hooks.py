"""Codex-owned native monitoring hooks; never approves trust or reads transcripts."""
from __future__ import annotations

import argparse
import copy
import hashlib
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


def _orca_runtime_root(root):
    """Orca mirrors the system Codex root; it must never be a migration writer."""
    return tuple(part.lower() for part in Path(root).parts[-3:]) == ('orca', 'codex-runtime-home', 'home')


def _canonical_mcp_groups(server):
    generated = generated_hooks(server, subagent_activity=True)
    return {event: generated[event][0] for event in EVENTS}


def _owned_group(group, event, server):
    return (isinstance(group, dict) and isinstance(group.get('hooks'), list) and any(
        isinstance(handler, dict) and handler.get('type') == 'mcp_tool'
        and handler.get('server') == server and handler.get('tool') == TOOL
        for handler in group['hooks']))


def _migration_plan(document, config, server):
    """Return JSON after removing exactly the eight current standalone groups.

    A matching inline set means an earlier successful migration and is idempotent.
    Any partial, duplicate, or edited owned definition is deliberately ambiguous.
    """
    if not isinstance(document, dict) or not isinstance(document.get('hooks', {}), dict):
        raise ValueError('hooks document must be an object')
    canonical = _canonical_mcp_groups(server)
    inline = config.get('hooks', {})
    if not isinstance(inline, dict):
        raise HookConflict('invalid-inline-hooks')
    inline_owned = {event: [group for group in inline.get(event, []) if _owned_group(group, event, server)]
                    for event in EVENTS}
    inline_count = sum(map(len, inline_owned.values()))
    if inline_count:
        if any(len(inline_owned[event]) != 1 or inline_owned[event][0] != canonical[event] for event in EVENTS):
            raise HookConflict('modified-or-partial-inline-monitor-definition-preserved')
        # JSON must already have no owned definition. Otherwise two active sources exist.
        if any(_owned_group(group, event, server) for event in EVENTS
               for group in document['hooks'].get(event, [])):
            raise HookConflict('duplicate-inline-and-json-monitor-definition-preserved')
        return copy.deepcopy(document), False
    result = copy.deepcopy(document)
    for event in EVENTS:
        groups = result['hooks'].get(event)
        if not isinstance(groups, list):
            raise HookConflict('missing-monitor-definition-preserved')
        matches = [group for group in groups if _owned_group(group, event, server)]
        if len(matches) != 1 or matches[0] != canonical[event]:
            raise HookConflict('modified-or-duplicate-monitor-definition-preserved')
        # Keep the array slot as an inert group. Orca keys unrelated command
        # approvals by source group index; deleting index 0 would re-key every
        # following user handler and discard its existing trust decision.
        result['hooks'][event] = [({'hooks': []} if group is matches[0] else group) for group in groups]
    return result, True


def _toml_mcp_groups(server):
    lines = ['# Engram-owned native Codex MCP monitor hooks. Do not edit these groups.']
    for event, group in _canonical_mcp_groups(server).items():
        handler = group['hooks'][0]
        lines += [f'[[hooks.{event}]]', f'[[hooks.{event}.hooks]]',
                  'type = "mcp_tool"', f'server = {json.dumps(handler["server"])}',
                  f'tool = {json.dumps(handler["tool"])}', 'timeout = 2', '[hooks.' + event + '.hooks.input]']
        for key, value in handler['input'].items():
            lines.append(f'{key} = {json.dumps(value)}')
        lines.append('')
    return '\n'.join(lines)


def _atomic_bytes(path, body):
    fd, temporary = tempfile.mkstemp(prefix='.engram-codex-migration-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _immutable_backup(path, body):
    with path.open('xb') as stream:
        stream.write(body); stream.flush(); os.fsync(stream.fileno())
    if path.read_bytes() != body:
        raise ValueError('backup verification failed')


def _recover_interrupted_migration(root, config_path, hooks_path, *, apply):
    """Rollback only the known config-first partial state recorded by our journal."""
    journal = root / '.engram-codex-hook-migration.json'
    if not journal.exists():
        return
    record = json.loads(_read(journal).strip())
    token = str(record.get('token', ''))
    names = (str(record.get('config_backup', '')), str(record.get('hooks_backup', '')))
    expected = (f'config.toml.engram-monitor-backup-{token}', f'hooks.json.engram-monitor-backup-{token}')
    if not re.fullmatch(r'[0-9a-f]{32}', token) or names != expected:
        raise ValueError('invalid migration journal')
    config_backup, hooks_backup = (root / name for name in names)
    if not config_backup.is_file() or not hooks_backup.is_file():
        raise ValueError('incomplete migration journal')
    before_config, before_hooks = config_backup.read_bytes(), hooks_backup.read_bytes()
    if (hashlib.sha256(before_config).hexdigest() != record.get('config_sha256') or
            hashlib.sha256(before_hooks).hexdigest() != record.get('hooks_sha256')):
        raise ValueError('migration backup hash mismatch')
    current_config, current_hooks = _read(config_path, binary=True), _read(hooks_path, binary=True)
    if current_config == before_config and current_hooks == before_hooks:
        if apply: journal.unlink()
        return
    if (hashlib.sha256(current_config).hexdigest() == record.get('new_config_sha256') and
            hashlib.sha256(current_hooks or b'').hexdigest() == record.get('new_hooks_sha256')):
        if apply: journal.unlink()
        return
    # The only automatic recovery is the safe, observed config-first partial:
    # JSON remains exactly original and TOML now contains our complete groups.
    parsed = tomllib.loads(current_config.decode('utf-8-sig'))
    inline = parsed.get('hooks', {})
    if current_hooks == before_hooks and all(
        inline.get(event, []).count(group) == 1 for event, group in _canonical_mcp_groups('engram').items()):
        if not apply:
            raise ValueError('incomplete migration requires apply recovery')
        _atomic_bytes(config_path, before_config); journal.unlink()
        return
    raise ValueError('incomplete migration requires manual recovery')


def configure_root(root, *, server='engram', apply=False, inspect=True, upgrade_native_identity=False, upgrade_subagent_activity=False, migration=False):
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
    if _orca_runtime_root(root):
        return {**result, 'reason': 'orca-runtime-root-deferred-to-system-root',
                'status': 'deferred', 'review_instruction':
                'Orca mirrors the system Codex configuration. Migrate the system ~/.codex root, then refresh Orca.'}
    if migration:
        _recover_interrupted_migration(root, config_path, root / 'hooks.json', apply=apply)
    config_before = _read(config_path, binary=True)
    config = tomllib.loads(config_before.decode('utf-8-sig'))
    features = config.get('features', {})
    if features.get('hooks', features.get('codex_hooks', True)) is False:
        return {**result, 'reason': 'user-disabled-hooks', 'status': 'disabled'}
    path = root / 'hooks.json'; original = _read(path, binary=True)
    document = json.loads(original.decode('utf-8-sig')) if original is not None else {'hooks': {}}
    if not migration:
        inline_owned = any(_owned_group(group, event, server) for event in EVENTS
                           for group in config.get('hooks', {}).get(event, []))
        if inline_owned:
            try:
                _migration_plan(document, config, server)
            except HookConflict:
                return {**result, 'reason': 'inline-monitor-hooks-preserved', 'conflict': True,
                        'status': 'conflict'}
            status = inspect_root(root, server=server) if inspect else {'status':'unknown','trust_required':False,'reason':'native-status-not-inspected'}
            return {**result, **status, 'changed': False, 'applied': False, 'hook_count': len(EVENTS) + 1}
        try:
            merged = merge_settings(document, server, upgrade_native_identity=upgrade_native_identity,
                                    upgrade_subagent_activity=upgrade_subagent_activity or original is None)
        except HookConflict:
            return {**result, 'reason': 'modified-monitor-definition-preserved', 'conflict': True,
                    'status': 'conflict', 'review_instruction':
                    'User-modified monitor hooks were preserved. Inspect the definitions before changing them. '
                    + result['review_instruction']}
        changed = merged != document
        if apply and changed:
            if original is not None:
                with path.with_name(path.name + '.engram-monitor-backup-' + uuid.uuid4().hex).open('xb') as stream: stream.write(original)
            if _read(path, binary=True) != original or _read(config_path, binary=True) != config_before:
                raise ValueError('configuration changed during apply')
            _atomic_bytes(path, (json.dumps(merged, ensure_ascii=False, indent=2) + '\n').encode())
        status = inspect_root(root, server=server) if inspect and (apply or not changed) else {'status':'unknown','trust_required':False,'reason':'planned-hooks-not-inspected'}
        return {**result, **status, 'changed': changed, 'applied': bool(apply), 'hook_count': len(EVENTS) + 1}
    try:
        inline_owned = any(_owned_group(group, event, server) for event in EVENTS
                           for group in config.get('hooks', {}).get(event, []))
        if original is None and not inline_owned:
            merged, migrate = {'hooks': {'SessionStart': generated_hooks(server)['SessionStart']}}, True
        else:
            merged, migrate = _migration_plan(document, config, server)
    except HookConflict:
        return {**result, 'reason': 'modified-monitor-definition-preserved', 'conflict': True,
                'status': 'conflict', 'review_instruction':
                'User-modified monitor hooks were preserved. Inspect the definitions in '
                + str(path) + ' before changing them. ' + result['review_instruction']}
    changed = migrate
    if apply and changed:
        token = uuid.uuid4().hex
        config_backup = config_path.with_name(config_path.name + '.engram-monitor-backup-' + token)
        hooks_backup = path.with_name(path.name + '.engram-monitor-backup-' + token)
        # Exclusive creation makes a UUID collision or pre-existing recovery
        # artifact a hard stop instead of overwriting a user's only snapshot.
        _immutable_backup(config_backup, config_before)
        _immutable_backup(hooks_backup, original or b'{}\n')
        # Keep every original config byte, including whitespace following
        # [hooks.state], and add only a syntactic separator plus owned tables.
        separator = b'\n' if config_before.endswith(b'\n') else b'\n\n'
        new_config = config_before + separator + _toml_mcp_groups(server).encode('utf-8') + b'\n'
        new_json = (json.dumps(merged, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        journal = root / '.engram-codex-hook-migration.json'
        record = {'phase': 'prepared', 'token': token, 'config_sha256': hashlib.sha256(config_before).hexdigest(),
                  'hooks_sha256': hashlib.sha256(original or b'{}\n').hexdigest(),
                  'new_config_sha256': hashlib.sha256(new_config).hexdigest(),
                  'new_hooks_sha256': hashlib.sha256(new_json).hexdigest(),
                  'config_backup': config_backup.name, 'hooks_backup': hooks_backup.name}
        _atomic_bytes(journal, (json.dumps(record, sort_keys=True) + '\n').encode())
        if _read(path, binary=True) != original or _read(config_path, binary=True) != config_before:
            raise ValueError('configuration changed during apply')
        _atomic_bytes(config_path, new_config)
        record['phase'] = 'config-written'; _atomic_bytes(journal, (json.dumps(record, sort_keys=True) + '\n').encode())
        _atomic_bytes(path, new_json)
        record['phase'] = 'complete'; _atomic_bytes(journal, (json.dumps(record, sort_keys=True) + '\n').encode())
        # A completed migration needs only its immutable snapshots.  Leaving a
        # journal would make later legitimate Codex edits look interrupted.
        journal.unlink()
    status = inspect_root(root, server=server) if inspect and (apply or not changed) else {
        'status': 'unknown', 'trust_required': False, 'reason': 'planned-hooks-not-inspected'}
    return {**result, **status, 'changed': changed, 'applied': bool(apply),
            'hook_count': len(EVENTS) + 1}


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


def migrate_system_root(*, home=None, server='engram', apply=False, inspect=True):
    """Migrate only ~/.codex; Orca projects this root into its runtime home."""
    root = (Path.home() if home is None else Path(home)) / '.codex'
    result = configure_root(root, server=server, apply=apply, inspect=inspect, migration=True)
    return {'authority': 'system-codex-root', 'root': str(root), 'result': result,
            'trust_autoapproved': False}


def repair_migrated_system_root(*, home=None, server='engram', apply=False):
    """Restore inert JSON slots after an older migration deleted them.

    This is intentionally narrow: an original immutable backup, the exact
    inline target, and the exact old deletion-form JSON must all agree.
    """
    root = (Path.home() if home is None else Path(home)) / '.codex'
    config_path, hooks_path = root / 'config.toml', root / 'hooks.json'
    config_bytes, current = _read(config_path, binary=True), _read(hooks_path, binary=True)
    result = {'root': str(root), 'changed': False, 'applied': False, 'trust_autoapproved': False}
    if config_bytes is None or current is None: return {**result, 'reason': 'repair-source-missing'}
    config, current_doc = tomllib.loads(config_bytes.decode('utf-8-sig')), json.loads(current.decode('utf-8-sig'))
    candidates = []
    for backup in root.glob('hooks.json.engram-monitor-backup-*'):
        token = backup.name.removeprefix('hooks.json.engram-monitor-backup-')
        paired = root / ('config.toml.engram-monitor-backup-' + token)
        if re.fullmatch(r'[0-9a-f]{32}', token) and paired.is_file(): candidates.append(backup)
    if len(candidates) != 1: return {**result, 'reason': 'repair-backup-ambiguous'}
    original = json.loads(candidates[0].read_text(encoding='utf-8-sig'))
    canonical = _canonical_mcp_groups(server)
    inline = config.get('hooks', {})
    if any(inline.get(event, []).count(group) != 1 for event, group in canonical.items()):
        return {**result, 'reason': 'repair-semantic-mismatch'}
    desired, migrated = copy.deepcopy(original), True
    for event, group in canonical.items():
        matches = [item for item in desired.get('hooks', {}).get(event, []) if _owned_group(item, event, server)]
        if len(matches) != 1 or matches[0] != group:
            return {**result, 'reason': 'repair-semantic-mismatch'}
        desired['hooks'][event] = [({'hooks': []} if item is matches[0] else item)
                                   for item in desired['hooks'][event]]
    legacy = copy.deepcopy(original)
    for event in EVENTS:
        legacy['hooks'][event] = [group for group in legacy['hooks'][event]
                                  if not _owned_group(group, event, server)]
    if not migrated or current_doc != legacy:
        return {**result, 'reason': 'repair-current-json-mismatch'}
    result['changed'] = True
    if apply:
        backup = hooks_path.with_name(hooks_path.name + '.engram-monitor-repair-backup-' + uuid.uuid4().hex)
        _immutable_backup(backup, current)
        if _read(config_path, binary=True) != config_bytes or _read(hooks_path, binary=True) != current:
            raise ValueError('configuration changed during repair')
        _atomic_bytes(hooks_path, (json.dumps(desired, ensure_ascii=False, indent=2) + '\n').encode())
        result['applied'] = True
    return {**result, 'reason': 'repair-ready'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, action='append')
    parser.add_argument('--server', default='engram')
    parser.add_argument('--provision', action='store_true')
    parser.add_argument('--migrate-system-root', action='store_true',
                        help='Migrate only ~/.codex hooks.json into inline config.toml hooks.')
    parser.add_argument('--repair-migrated-system-root', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--upgrade-native-identity', action='store_true', help='Explicit one-time hook identity upgrade; changed hooks need native trust review.')
    parser.add_argument('--upgrade-subagent-activity', action='store_true', help='Explicit child lifecycle upgrade; implies native identity and requires hook review.')
    args = parser.parse_args(argv)
    try:
        result = (repair_migrated_system_root(server=args.server, apply=args.apply)
                  if args.repair_migrated_system_root else migrate_system_root(server=args.server, apply=args.apply)
                  if args.migrate_system_root else provision(args.root, server=args.server, apply=args.apply, upgrade_native_identity=args.upgrade_native_identity, upgrade_subagent_activity=args.upgrade_subagent_activity))
        print(json.dumps(result))
        review_roots = result.get('roots', [result['result']] if 'result' in result else [])
        for root in review_roots:
            if root.get('status') != 'ready':
                print('Codex hooks: ' + root['status'] + ' (' + root['reason'] + '). '
                      + root['review_instruction'], file=sys.stderr)
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit('Codex hook configuration failed: ' + type(exc).__name__) from None


if __name__ == '__main__':
    main()
