"""Narrow bridge registration; never rewrites hook definitions or approvals."""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib
import uuid


def _read(path):
    value = path.read_bytes() if path.exists() else b''
    if len(value) > 4_000_000:
        raise ValueError('configuration too large')
    return value


def _write(path, before, after):
    if before == after:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if _read(path) != before:
        raise RuntimeError('configuration changed during preparation')
    if path.exists():
        backup = path.with_name(path.name + '.engram-bridge-backup-' + uuid.uuid4().hex)
        with backup.open('xb') as f:
            f.write(before)
    fd, temporary = tempfile.mkstemp(prefix='.engram-bridge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(after)
            f.flush()
            os.fsync(f.fileno())
        if _read(path) != before:
            raise RuntimeError('configuration changed during preparation')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def _entry(old, command, args, *, codex=False, timeout=120):
    if not isinstance(old, dict):
        raise ValueError('invalid Engram MCP entry')
    result = copy.deepcopy(old)
    for key in ('type', 'url', 'http_headers', 'env_http_headers', 'bearer_token_env_var', 'headers'):
        result.pop(key, None)
    result.update(command=str(command), args=list(args))
    if codex:
        result['startup_timeout_sec'] = max(timeout, result.get('startup_timeout_sec', 0))
    else:
        result['type'] = 'stdio'
    return result


def merge_claude(path, command, args):
    path = Path(path)
    before = _read(path)
    data = json.loads(before.decode('utf-8-sig')) if before else {}
    if not isinstance(data, dict) or not isinstance(data.get('mcpServers', {}), dict):
        raise ValueError('invalid Claude configuration')
    old = copy.deepcopy(data)
    servers = data.setdefault('mcpServers', {})
    servers['engram'] = _entry(servers.get('engram', {}), command, args)
    # Existing per-project copies would shadow the global transport. Keep all
    # project approvals/enablement untouched, replacing only existing engram.
    projects = data.get('projects', {})
    if isinstance(projects, dict):
        for project in projects.values():
            entries = project.get('mcpServers') if isinstance(project, dict) else None
            if isinstance(entries, dict) and 'engram' in entries:
                entries['engram'] = _entry(entries['engram'], command, args)
    if old == data:
        return False
    after = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    return _write(path, before, after)


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if type(value) is bool:
        return str(value).lower()
    if type(value) in (int, float):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return '[' + ', '.join(_toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(k) + ' = ' + _toml_value(v) for k, v in value.items()) + '}'
    raise ValueError('unsupported TOML value')


def _header_path(header):
    try:
        data = tomllib.loads(header + '\n__engram_bridge_probe = true\n')
    except tomllib.TOMLDecodeError:
        return ()
    path = []
    while isinstance(data, dict) and '__engram_bridge_probe' not in data and len(data) == 1:
        key, data = next(iter(data.items()))
        path.append(key)
    return tuple(path)


def merge_codex(path, command, args, timeout=120):
    path = Path(path)
    before = _read(path)
    text = before.decode('utf-8-sig')
    data = tomllib.loads(text)
    servers = data.get('mcp_servers', {})
    if not isinstance(servers, dict):
        raise ValueError('invalid MCP table')
    replacement = _entry(servers.get('engram', {}), command, args, codex=True, timeout=timeout)
    if servers.get('engram') == replacement:
        return False
    expected = copy.deepcopy(data)
    expected.setdefault('mcp_servers', {})['engram'] = replacement
    # Retain every byte outside the owned table, including hook trust provenance.
    headers = list(re.finditer(r'(?m)^[ \t]*(\[\[?[^\r\n]+?\]\]?)[ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)', text))
    spans = []
    for index, match in enumerate(headers):
        if _header_path(match.group(1))[:2] == ('mcp_servers', 'engram'):
            spans.append((match.start(), headers[index + 1].start() if index + 1 < len(headers) else len(text)))
    if 'engram' in servers and not spans:
        raise ValueError('inline Engram table requires explicit migration')
    newline = '\r\n' if '\r\n' in text else '\n'
    block = '[mcp_servers.engram]' + newline + ''.join(
        json.dumps(k) + ' = ' + _toml_value(v) + newline for k, v in replacement.items()) + newline
    if spans:
        pieces, end = [], 0
        for index, (start, stop) in enumerate(spans):
            pieces.append(text[end:start])
            if index == 0:
                pieces.append(block)
            end = stop
        pieces.append(text[end:])
        after_text = ''.join(pieces)
    else:
        after_text = text + (newline if text and not text.endswith('\n') else '') + newline + block
    if tomllib.loads(after_text) != expected:
        raise ValueError('configuration preservation check failed')
    bom = b'\xef\xbb\xbf' if before.startswith(b'\xef\xbb\xbf') else b''
    return _write(path, before, bom + after_text.encode('utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--claude-config', action='append', default=[])
    parser.add_argument('--codex-config', action='append', default=[])
    parser.add_argument('--auto-home', action='store_true')
    parser.add_argument('--command', required=True)
    parser.add_argument('--arg', action='append', default=[])
    options = parser.parse_args(argv)
    if options.auto_home:
        from core.install.codex_monitor_hooks import existing_roots
        options.claude_config += [str(Path.home()/'.claude.json'), str(Path.home()/'.engram/claude-mcp.json')]
        options.codex_config += [str(root/'config.toml') for root in existing_roots()]
    counts = {'changed': 0, 'unchanged': 0}
    for merge, paths in ((merge_claude, options.claude_config), (merge_codex, options.codex_config)):
        for path in dict.fromkeys(paths):
            changed = merge(path, options.command, options.arg)
            counts['changed' if changed else 'unchanged'] += 1
    print(json.dumps(counts))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
