"""One-time native presentation migration; user assets and other renderers survive."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

import yaml

VERSION = 1
MARKER = 'native_bolttagu_migration'
NATIVE_MODE = 'native_bolttagu'
EXTERNAL_ID = 'engram.bolttagu-2d'


def store_mapping(document: dict, directory: Path) -> Path:
    """Content-addressed owned copy; no existing source or destination is overwritten."""
    from overlay.bolttagu_mapping import validate_mapping_document
    validate_mapping_document(document)
    content = json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')
    if len(content) > 65536:
        raise ValueError('mapping is too large')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (hashlib.sha256(content).hexdigest() + '.json')
    descriptor, temporary = tempfile.mkstemp(prefix='.mapping-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Atomic no-clobber publication on NTFS/POSIX: the complete temporary
            # inode becomes visible at the final name, never a partially written file.
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != content:
                raise ValueError('owned mapping destination conflict')
    finally:
        os.unlink(temporary)
    return target


def import_mapping(source: Path, directory: Path) -> Path:
    if source.stat().st_size > 65536:
        raise ValueError('mapping is too large')
    content = source.read_bytes()
    document = json.loads(content.decode('utf-8-sig'))
    return store_mapping(document, directory)


def pending_mapping_warning(path: Path, document: dict) -> str:
    overlay = document.get('overlay', {})
    if not isinstance(overlay, dict) or overlay.get(MARKER) == VERSION:
        return ''
    character = overlay.get('character', {})
    if isinstance(character, dict) and isinstance(character.get('bolttagu', {}), dict) and character.get('bolttagu', {}).get('mapping_path'):
        return ''
    source = path.parent / 'overlays' / 'bolttagu-2d' / 'mapping.json'
    if not source.is_file():
        return ''
    from overlay.bolttagu_mapping import validate_mapping_file
    try:
        validate_mapping_file(source)
        return ''
    except (OSError, ValueError) as exc:
        return '외부 볼따구 매핑 이전 대기: ' + str(exc)[:180]


def migrated_config(document: dict) -> tuple[dict, bool]:
    result = copy.deepcopy(document)
    if not isinstance(result, dict):
        raise ValueError('overlay config must be a mapping')
    overlay = result.setdefault('overlay', {})
    if not isinstance(overlay, dict):
        raise ValueError('overlay must be a mapping')
    marker = overlay.get(MARKER)
    if marker is not None:
        if type(marker) is not int or marker != VERSION:
            raise ValueError('unsupported native migration version')
        return result, False
    from core.install.user_config import preserve_legacy_character_source_mode
    preserve_legacy_character_source_mode(result)
    character = overlay.setdefault('character', {})
    if not isinstance(character, dict):
        raise ValueError('character must be a mapping')
    external = overlay.get('external_renderer', {})
    if not isinstance(external, dict):
        raise ValueError('external renderer must be a mapping')
    selected = external.get('selected_renderer_id', '')
    if not isinstance(selected, str):
        raise ValueError('renderer identity must be a string')
    if selected == EXTERNAL_ID:
        if external.get('mode', 'observer') not in ('observer', 'replace'):
            raise ValueError('unsupported external Bolttagu mode')
        external['selected_renderer_id'] = ''
        external['mode'] = 'observer'
    elif 'bolttagu' in selected.casefold():
        raise ValueError('unrecognized Bolttagu renderer identity')
    old = character.get('source_mode')
    if old not in (None, '', 'static', 'sequence', 'sprite_grid', NATIVE_MODE):
        raise ValueError('unsupported character source mode')
    # Keep the previous source selection as well as every path/reaction field.
    if old and old != NATIVE_MODE:
        character.setdefault('legacy_source_mode', old)
    character['source_mode'] = NATIVE_MODE
    overlay[MARKER] = VERSION
    return result, True


def migrate_file(path: Path, *, checkpoint=None) -> bool:
    """Unique backup + compare-before-replace. No parent/asset/startup modifications."""
    if not path.is_file():
        return False
    before = path.read_bytes()
    document = yaml.safe_load(before.decode('utf-8-sig')) or {}
    result, changed = migrated_config(document)
    if not changed:
        return False
    source = path.parent / 'overlays' / 'bolttagu-2d' / 'mapping.json'
    options = result['overlay']['character'].setdefault('bolttagu', {})
    if not isinstance(options, dict):
        raise ValueError('native Bolttagu options must be a mapping')
    if not options.get('mapping_path') and source.is_file():
        options['mapping_path'] = str(import_mapping(source, path.parent / 'native-bolttagu' / 'mappings'))
    after = yaml.safe_dump(result, allow_unicode=True, sort_keys=False).encode('utf-8')
    backup = path.with_name(path.name + '.native-bolttagu-' + uuid.uuid4().hex + '.bak')
    with backup.open('xb') as stream:
        stream.write(before)
        stream.flush()
        os.fsync(stream.fileno())
    if checkpoint:
        checkpoint('backup')
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != before:
            raise RuntimeError('overlay config changed during migration')
        os.replace(temporary, path)
        if checkpoint:
            checkpoint('replace')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True
