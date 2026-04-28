# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['engram_overlay_entry.py'],
    pathex=[],
    binaries=[],
    datas=[('resource\\icon.png', 'resource'), ('resource\\overlay.png', 'resource'), ('resource\\character', 'resource\\character'), ('config\\overlay.yaml', 'config')],
    hiddenimports=['core.context_builder', 'core.db', 'core.identity', 'core.memory', 'core.directives', 'core.reflection', 'core.curiosity', 'core.sanitizer', 'core.memory_bus', 'core.runtime_config', 'core.stm_promoter', 'core.activity', 'core.project_scope', 'discord_bot', 'discord_bot.bot'],
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
