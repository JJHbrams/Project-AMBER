"""Fail-closed, same-project service handover before a new overlay starts.

HTTP health identifies a role, not ownership. Every destructive operation also
checks the actual TCP listener and OS process identity including creation time.
"""
from __future__ import annotations

import json
import ctypes
import socket
import hashlib
import configparser
from urllib.parse import urlsplit
import time
import urllib.request
from pathlib import Path

from core.install import process_identity as identity


def repository_identity(root: Path) -> Path | None:
    marker = root / '.git'
    try:
        if marker.is_dir():
            return marker.resolve()
        text = marker.read_text(encoding='utf-8').strip()
        if not text.startswith('gitdir:'):
            return None
        git_dir = (root / text.split(':', 1)[1].strip()).resolve()
        common = git_dir / 'commondir'
        return (git_dir / common.read_text(encoding='utf-8').strip()).resolve() if common.is_file() else git_dir
    except (OSError, ValueError):
        return None


def source_root_for_process(process: dict) -> Path | None:
    tokens = identity._tokens(str(process.get('CommandLine') or ''))
    if not tokens or not identity.same_path(tokens[0], process.get('ExecutablePath', '')):
        return None
    if Path(process.get('ExecutablePath', '')).name.casefold() not in ('python.exe', 'pythonw.exe'):
        return None
    for token in tokens[1:2]:
        path = Path(token)
        if path.name == 'engram_overlay_entry.py' and path.is_file():
            return path.resolve().parent
    return None


def repository_fingerprint(root: Path) -> str:
    """Stable repository identity without publishing paths or remote credentials."""
    common = repository_identity(root)
    if common is None:
        return ''
    try:
        parser = configparser.ConfigParser()
        parser.read(common / 'config', encoding='utf-8')
        remote = parser.get('remote "origin"', 'url')
        if '://' in remote:
            parts = urlsplit(remote)
            normalized = (parts.hostname or '') + '/' + parts.path.strip('/')
        else:
            normalized = remote.split('@')[-1].replace(':', '/', 1).strip('/')
        return hashlib.sha256(normalized.removesuffix('.git').casefold().encode()).hexdigest()
    except (OSError, ValueError, configparser.Error):
        return ''


def _artifact_fingerprint(executable: str) -> str:
    try:
        payload = json.loads(Path(executable).with_name('build-manifest.json').read_text(encoding='utf-8'))
        return str(payload.get('source_repository', ''))
    except (OSError, ValueError):
        return ''


def same_project_origin(old_root: Path, source_root: Path, executable: str = '') -> bool:
    project = repository_identity(source_root)
    if identity.same_path(old_root, source_root) or (project is not None and project == repository_identity(old_root)):
        return True
    fingerprint = _artifact_fingerprint(executable) if executable else ''
    return bool(fingerprint) and fingerprint == repository_fingerprint(old_root)


def is_project_host(process: dict, source_root: Path, executable: str) -> bool:
    tokens = identity._tokens(str(process.get('CommandLine') or ''))
    path = str(process.get('ExecutablePath') or '')
    if not process.get('CreationDate') or not tokens or not identity.same_path(tokens[0], path) or '--role' in tokens:
        return False
    old_root = source_root_for_process(process)
    if old_root:
        return same_project_origin(old_root, source_root, executable)
    if Path(path).name.casefold() != 'engram-overlay.exe':
        return False
    if identity.same_path(path, executable):
        return True
    if any(identity.same_path(path, trusted) for trusted in identity.trusted_installed_paths()['overlay']):
        return True
    # A source INSTALL artifact can be nested below any worktree in this repo.
    artifact_root = Path(path).parent.parent.parent
    return (Path(path).parent.name == 'engram-overlay' and Path(path).parent.parent.name == 'dist'
            and same_project_origin(artifact_root, source_root, executable))


def listener_pid(port: int) -> int | None:
    # Native owner-PID tables avoid shell parsing and optional dependencies.
    class Row4(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in ('state', 'address', 'port', 'remote', 'remote_port', 'pid')]
    class Row6(ctypes.Structure):
        _fields_ = [('address', ctypes.c_ubyte * 16), ('scope', ctypes.c_uint32), ('port', ctypes.c_uint32),
                    ('remote', ctypes.c_ubyte * 16), ('remote_scope', ctypes.c_uint32), ('remote_port', ctypes.c_uint32),
                    ('state', ctypes.c_uint32), ('pid', ctypes.c_uint32)]
    matches = set()
    for family, row_type in ((2, Row4), (23, Row6)):
        size = ctypes.c_ulong(0)
        api = ctypes.windll.iphlpapi.GetExtendedTcpTable
        result = api(None, ctypes.byref(size), False, family, 3, 0)
        if result not in (0, 122):
            raise RuntimeError(f'Cannot inspect listener ownership (Windows error {result})')
        buffer = ctypes.create_string_buffer(size.value)
        result = api(buffer, ctypes.byref(size), False, family, 3, 0)
        if result != 0:
            raise RuntimeError(f'Cannot inspect listener ownership (Windows error {result})')
        count = ctypes.c_uint32.from_buffer(buffer).value
        for index in range(count):
            row = row_type.from_buffer(buffer, 4 + index * ctypes.sizeof(row_type))
            if socket.ntohs(row.port & 0xffff) == port:
                matches.add(int(row.pid))
    if not matches:
        return None
    if len(matches) != 1 or None in matches:
        raise RuntimeError(f'Cannot verify unique listener ownership on port {port}')
    return matches.pop()


def health(port: int) -> dict:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as response:
        return json.loads(response.read())


def _still_same(process: dict) -> bool:
    current = identity.get_process_identity(int(process['ProcessId']), strict=True)
    return current is not None and identity._same_process(process, current)


def prepare_service_handover(cfg: dict, source_root: Path, executable: str, *, timeout: float = 20) -> None:
    port = int(cfg['overlay']['stm_server_port'])
    owner = listener_pid(port)
    if owner is None:
        _clear_proven_orphan_mcp(cfg, source_root, executable)
        return
    info = health(port)
    old = identity.get_process_identity(owner, strict=True)
    if info.get('role') != 'overlay-stm' or info.get('pid') != owner or old is None or not is_project_host(old, source_root, executable):
        raise RuntimeError(f'Port {port} belongs to an unverified or different-project process; nothing stopped')
    old_root = source_root_for_process(old)
    def child_owned(child: dict) -> bool:
        if old_root is not None:
            return identity.is_same_checkout_source_child(child, old_root)
        tokens = identity._tokens(str(child.get('CommandLine') or ''))
        if identity.same_path(child.get('ExecutablePath', ''), Path(old['ExecutablePath']).with_name('engram-dashboard.exe')):
            return bool(tokens) and identity.same_path(tokens[0], child['ExecutablePath']) and '--role' not in tokens
        return (identity.same_path(child.get('ExecutablePath', ''), old['ExecutablePath'])
                and '--role' in tokens and any(role in tokens for role in ('mcp-server', 'kg-watcher')))
    children = [child for child in identity.list_candidate_processes(strict=True)
                if child.get('ParentProcessId') == owner and child_owned(child)]
    # Validate every required service BEFORE asking the old host to exit.
    required = [int(cfg['mcp']['http_port'])]
    dashboard = cfg.get('dashboard') or {}
    if dashboard.get('enabled', True):
        required.append(int(dashboard.get('port', 8501)))
    for required_port in required:
        required_owner = listener_pid(required_port)
        if required_owner is None:
            continue
        child = next((candidate for candidate in children if candidate['ProcessId'] == required_owner), None)
        if child is None or not _still_same(child):
            raise RuntimeError(f'Service port {required_port} belongs to another process family; nothing stopped')
    if listener_pid(port) != owner or not _still_same(old):
        raise RuntimeError('Overlay listener identity changed during preflight; nothing stopped')
    request = urllib.request.Request(f'http://127.0.0.1:{port}/shutdown', data=b'{}',
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=3):
            pass
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while _still_same(old) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _still_same(old) and not identity.terminate_identity_exact(old, lambda p: is_project_host(p, source_root, executable)):
        raise RuntimeError('Verified old overlay did not exit; refusing to start another host')
    for child in children:
        if _still_same(child) and not identity.terminate_identity_exact(child, child_owned):
            raise RuntimeError('Verified old overlay child did not exit; refusing stale-service adoption')
    if listener_pid(port) is not None:
        raise RuntimeError('STM port is still occupied after handover')
    _clear_proven_orphan_mcp(cfg, source_root, executable)


def _clear_proven_orphan_mcp(cfg: dict, source_root: Path, executable: str = '') -> None:
    port = int(cfg['mcp']['http_port'])
    owner = listener_pid(port)
    if owner is None:
        dashboard = cfg.get('dashboard') or {}
        if dashboard.get('enabled', True) and listener_pid(int(dashboard.get('port', 8501))) is not None:
            raise RuntimeError('Dashboard port belongs to an unverified family; nothing stopped')
        return
    child = identity.get_process_identity(owner, strict=True)
    if child is None or not child.get('CreationDate'):
        raise RuntimeError('Unverified existing MCP listener; refusing reuse')
    tokens = identity._tokens(child.get('CommandLine', ''))
    old_roots = [Path(t).resolve().parent for t in tokens[1:] if Path(t).name == 'mcp_server.py']
    valid = any(identity.is_same_checkout_source_child(child, root) and
                same_project_origin(root, source_root, executable)
                for root in old_roots)
    frozen_root = Path(child.get('ExecutablePath', '')).parent.parent.parent
    frozen_valid = (Path(child.get('ExecutablePath', '')).name == 'engram-overlay.exe' and
                    len(tokens) > 2 and tokens[1:3] == ['--role', 'mcp-server'] and
                    (identity.is_default_installed_frozen_child(child) or
                     same_project_origin(frozen_root, source_root, executable)))
    valid = valid or frozen_valid
    parent = identity.get_process_identity(int(child.get('ParentProcessId') or 0), strict=True)
    if not valid or parent is not None or listener_pid(port) != owner or not _still_same(child):
        raise RuntimeError('Existing MCP belongs to a live or unverified process family; nothing stopped')
    def sibling_owned(candidate: dict) -> bool:
        if candidate.get('ParentProcessId') != child.get('ParentProcessId') or not candidate.get('CreationDate'):
            return False
        if any(identity.is_same_checkout_source_child(candidate, root) for root in old_roots):
            return True
        if frozen_valid:
            sibling_tokens = identity._tokens(candidate.get('CommandLine', ''))
            if identity.same_path(candidate.get('ExecutablePath', ''), child['ExecutablePath']):
                return len(sibling_tokens) > 2 and sibling_tokens[1:3] in (['--role', 'mcp-server'], ['--role', 'kg-watcher'])
            return (identity.same_path(candidate.get('ExecutablePath', ''), Path(child['ExecutablePath']).with_name('engram-dashboard.exe')) and
                    bool(sibling_tokens) and '--role' not in sibling_tokens)
        return False
    family = [candidate for candidate in identity.list_candidate_processes(strict=True) if sibling_owned(candidate)]
    if not any(candidate['ProcessId'] == owner for candidate in family):
        family.append(child)
    dashboard = cfg.get('dashboard') or {}
    if dashboard.get('enabled', True):
        dashboard_owner = listener_pid(int(dashboard.get('port', 8501)))
        if dashboard_owner is not None and not any(candidate['ProcessId'] == dashboard_owner for candidate in family):
            raise RuntimeError('Dashboard belongs to a different family; nothing stopped')
    for candidate in family:
        if not _still_same(candidate):
            raise RuntimeError('Orphan family identity changed; nothing stopped')
    for candidate in family:
        if _still_same(candidate) and not identity.terminate_identity_exact(candidate, sibling_owned):
            raise RuntimeError('Proven orphan family failed to exit')
    if listener_pid(port) is not None:
        raise RuntimeError('Old MCP listener did not exit; refusing reuse')
