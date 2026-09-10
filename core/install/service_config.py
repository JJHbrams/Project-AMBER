"""Canonical service settings shared by entrypoint, host and runtime contract.

Service settings in user.config.yaml take precedence over overlay appearance
settings. This resolver never reads or executes renderer commands.
"""
from __future__ import annotations

import copy
from pathlib import Path


def merge_service_config(overlay_cfg: dict) -> dict:
    from core.config.runtime_config import _LEGACY_USER_CONFIG_PATH, _USER_CONFIG_PATH
    import yaml

    result = copy.deepcopy(overlay_cfg)
    runtime = {}
    # Only explicit canonical user settings override appearance-file settings;
    # runtime defaults must never erase an existing overlay.user custom port.
    for path in (_LEGACY_USER_CONFIG_PATH, _USER_CONFIG_PATH):
        if not path.is_file():
            continue
        document = yaml.safe_load(path.read_text(encoding='utf-8-sig')) or {}
        if not isinstance(document, dict):
            raise ValueError(f'Service config must be a mapping: {path}')
        for section in ('overlay', 'mcp', 'dashboard'):
            values = document.get(section)
            if isinstance(values, dict):
                runtime[section] = {**runtime.get(section, {}), **values}
    for section in ('mcp', 'dashboard'):
        canonical = runtime.get(section)
        if isinstance(canonical, dict):
            result[section] = {**(result.get(section) or {}), **canonical}
    canonical_overlay = runtime.get('overlay') or {}
    if 'stm_server_port' in canonical_overlay:
        result.setdefault('overlay', {})['stm_server_port'] = canonical_overlay['stm_server_port']
    ports = [('overlay', 'stm_server_port', 17384), ('mcp', 'http_port', 17385), ('dashboard', 'port', 8501)]
    for section, key, default in ports:
        raw = (result.get(section) or {}).get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ValueError(f'{section}.{key} must be an integer TCP port')
        value = int(raw)
        if not 1 <= value <= 65535:
            raise ValueError(f'{section}.{key} must be a valid TCP port')
        result.setdefault(section, {})[key] = value
    return result


def service_config_provenance() -> dict:
    from core.config.runtime_config import _USER_CONFIG_PATH, _LEGACY_USER_CONFIG_PATH
    return {'canonical': str(_USER_CONFIG_PATH.resolve()),
            'legacy': str(_LEGACY_USER_CONFIG_PATH.resolve()),
            'overlay': str((Path.home() / '.engram/overlay.user.yaml').resolve())}


def effective_service_config() -> dict:
    from overlay.config import load_cfg
    return load_cfg(strict=True, create_user_config=False)


def main(argv=None) -> int:
    import argparse
    import json
    argparse.ArgumentParser(description='Read-only effective service endpoint configuration').parse_args(argv)
    cfg = effective_service_config()
    print(json.dumps({'stm_port': cfg['overlay']['stm_server_port'], 'mcp_port': cfg['mcp']['http_port'],
                      'dashboard_port': cfg['dashboard']['port'], 'dashboard_enabled': bool(cfg['dashboard'].get('enabled', True)),
                      'service_config': service_config_provenance()}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
