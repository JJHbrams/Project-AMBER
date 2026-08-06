# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path


def _collect_tcl_tk() -> list[tuple[str, str]]:
    """Bundle Tcl/Tk data files so pyi_rth__tkinter.py can find _tcl_data/_tk_data."""
    candidates = [
        Path(sys.prefix) / 'Library' / 'lib',
        Path(sys.prefix) / 'tcl',
        Path(sys.prefix) / 'lib',
    ]
    items: list[tuple[str, str]] = []
    for base in candidates:
        if not base.exists():
            continue
        for d in base.iterdir():
            if not d.is_dir():
                continue
            n = d.name.lower()
            if n.startswith('tcl') and len(n) > 3 and n[3].isdigit():
                items.append((str(d), '_tcl_data'))
            elif n.startswith('tk') and len(n) > 2 and n[2].isdigit():
                items.append((str(d), '_tk_data'))
        if items:
            break
    return items


_tcl_tk_datas = _collect_tcl_tk()


def _collect_character_datas() -> list[tuple[str, str]]:
    """Collect runtime character assets while skipping editor lock files."""
    root = Path("resource") / "character"
    if not root.exists():
        return []

    excluded_suffixes = {".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx"}
    items: list[tuple[str, str]] = []
    for src in root.rglob("*"):
        if not src.is_file():
            continue
        if src.name.startswith("~$"):
            continue
        if src.suffix.lower() in excluded_suffixes:
            continue

        rel_parent = src.parent.relative_to(root)
        if str(rel_parent) == ".":
            dest = Path("resource") / "character"
        else:
            dest = Path("resource") / "character" / rel_parent
        items.append((str(src), str(dest)))
    return items


_character_datas = _collect_character_datas()


def _collect_embedding_model() -> list[tuple[str, str]]:
    """오프라인 임베딩 모델(resource/embedding-model) 을 있으면 번들.

    build-installer.ps1 이 SentenceTransformer.save() 로 미리 export 한다.
    없으면 빈 리스트 → 런타임에 HuggingFace Hub 폴백(개발 모드).
    """
    root = Path("resource") / "embedding-model"
    if not root.exists():
        return []
    items: list[tuple[str, str]] = []
    for src in root.rglob("*"):
        if not src.is_file():
            continue
        rel_parent = src.parent.relative_to(root)
        dest = Path("resource") / "embedding-model"
        if str(rel_parent) != ".":
            dest = dest / rel_parent
        items.append((str(src), str(dest)))
    return items


_embedding_model_datas = _collect_embedding_model()


a = Analysis(
    ['engram_overlay_entry.py'],
    # scripts/kg 를 추가해 멀티콜 백엔드용 kg_watcher 를 top-level 모듈로 수집한다.
    pathex=['scripts\\kg'],
    binaries=[],
    datas=[
        ('resource\\icon.png', 'resource'),
        ('resource\\overlay.png', 'resource'),
        *_character_datas,
        *_embedding_model_datas,
        ('config\\overlay.yaml', 'config'),
        # runtime_config(core/config/runtime_config.py)가 읽는 파일.
        # 누락 시 resolve_runtime_path 가 못 찾아 tools.disabled 등이 조용히 무시된다.
        ('config\\config.yaml', 'config'),
        *_tcl_tk_datas,
    ],
    hiddenimports=['core.context.context_builder', 'core.storage.db', 'core.identity', 'core.memory', 'core.context.directives', 'core.identity.reflection', 'core.identity.curiosity', 'core.common.sanitizer', 'core.memory.bus', 'core.config.runtime_config', 'core.config.remote_tokens', 'core.graph.semantic', 'core.graph.semantic.stm_promoter', 'core.observability.activity', 'core.context.project_scope', 'discord_bot', 'discord_bot.bot', 'tkinterweb', 'tkinterweb_tkhtml', 'mcp_server', 'kg_watcher'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name='engram-overlay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],  # UPX 비활성화 — 빌드 속도 우선
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['resource\\icon.png'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='engram-overlay',
)
