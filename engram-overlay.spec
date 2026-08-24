# -*- mode: python ; coding: utf-8 -*-

import json
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)


_version_snapshot_path = Path('build') / 'engram-version.json'
if not _version_snapshot_path.is_file():
    raise FileNotFoundError(f'Version snapshot missing: {_version_snapshot_path}')
_version_snapshot = json.loads(_version_snapshot_path.read_text(encoding='utf-8'))
_version_text = _version_snapshot['version']
_version_tuple = tuple(int(part) for part in _version_text.split('.'))
if len(_version_tuple) != 4:
    raise ValueError(f'Invalid four-part version: {_version_text}')
_windows_version = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=_version_tuple,
        prodvers=_version_tuple,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', 'DRTECH'),
            StringStruct('FileDescription', 'Engram Overlay'),
            StringStruct('FileVersion', _version_text),
            StringStruct('InternalName', 'engram-overlay'),
            StringStruct('OriginalFilename', 'engram-overlay.exe'),
            StringStruct('ProductName', 'Engram Overlay'),
            StringStruct('ProductVersion', _version_text),
        ])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])]),
    ],
)


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


def _collect_tk_python_runtime() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Collect tkinter even when PyInstaller's isolated Tcl probe is unreliable.

    Conda's regular Python process can load Tk while PyInstaller's isolated
    helper fails to source init.tcl.  The standard hook then suppresses the
    otherwise valid tkinter package.  We already collect Tcl/Tk scripts above;
    pair them explicitly with the stdlib package and extension module.
    """
    datas: list[tuple[str, str]] = []
    binaries: list[tuple[str, str]] = []
    package = Path(sys.prefix) / 'Lib' / 'tkinter'
    extension = Path(sys.prefix) / 'DLLs' / '_tkinter.pyd'
    if package.is_dir():
        datas.append((str(package), 'tkinter'))
    if extension.is_file():
        binaries.append((str(extension), '.'))
    return datas, binaries


_tk_python_datas, _tk_python_binaries = _collect_tk_python_runtime()


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

    installer/build-overlay.ps1 이 cache에서 검증·export 한다.
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
_streamlit_datas, _streamlit_binaries, _streamlit_hiddenimports = collect_all("streamlit")
_mcp_datas, _mcp_binaries, _mcp_hiddenimports = collect_all("mcp")


a = Analysis(
    ['engram_overlay_entry.py'],
    # scripts/kg 를 추가해 멀티콜 백엔드용 kg_watcher 를 top-level 모듈로 수집한다.
    pathex=['scripts\\kg'],
    binaries=[*_streamlit_binaries, *_mcp_binaries, *_tk_python_binaries],
    datas=[
        ('resource\\icon.png', 'resource'),
        ('resource\\overlay.png', 'resource'),
        *_character_datas,
        *_embedding_model_datas,
        ('config\\overlay.yaml', 'config'),
        # runtime_config(core/config/runtime_config.py)가 읽는 파일.
        # 누락 시 resolve_runtime_path 가 못 찾아 tools.disabled 등이 조용히 무시된다.
        ('config\\config.yaml', 'config'),
        (str(_version_snapshot_path), '.'),
        # Streamlit executes the dashboard entry as a raw source file.
        ('core\\dashboard\\app.py', 'core\\dashboard'),
        ('core\\dashboard\\assets', 'core\\dashboard\\assets'),
        *_streamlit_datas,
        *_mcp_datas,
        *_tcl_tk_datas,
        *_tk_python_datas,
    ],
    hiddenimports=['core.context.context_builder', 'core.storage.db', 'core.identity', 'core.memory', 'core.context.directives', 'core.identity.reflection', 'core.identity.curiosity', 'core.common.sanitizer', 'core.memory.bus', 'core.config.runtime_config', 'core.config.remote_tokens', 'core.graph.semantic', 'core.graph.semantic.stm_promoter', 'core.install.bootstrap', 'core.install.model_manifest', 'core.observability.activity', 'core.context.project_scope', 'core.dashboard.app', 'discord_bot', 'discord_bot.bot', 'tkinterweb', 'tkinterweb_tkhtml', 'mcp_server', 'kg_watcher', 'scripts.kg.kg_lint', *_streamlit_hiddenimports, *_mcp_hiddenimports],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['installer\\pyi_rth_engram_tk.py'],
    excludes=[],
    # one-dir 배포에서 지연 import 모듈을 PYZ(zlib)에서 읽다 실패하는 환경을 피한다.
    noarchive=True,
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
    version=_windows_version,
)

dashboard_exe = EXE(
    pyz,
    a.scripts,
    [],
    name='engram-dashboard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['resource\\icon.png'],
    version=_windows_version,
)

coll = COLLECT(
    exe,
    dashboard_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='engram-overlay',
)
