# Console-only standalone bridge, also collected by engram-overlay.spec.
from PyInstaller.utils.hooks import copy_metadata
bridge_a = Analysis(
    ['scripts/engram_mcp_bridge.py'], pathex=['.'],
    binaries=[], datas=copy_metadata('mcp'),
    hiddenimports=['anyio._backends._asyncio', 'core.install.mcp_bridge_config'],
    hookspath=[], runtime_hooks=[],
    excludes=['tkinter', 'torch', 'transformers', 'numpy', 'pandas', 'streamlit'],
    noarchive=False,
)
bridge_exe = EXE(
    PYZ(bridge_a.pure), bridge_a.scripts, bridge_a.binaries, bridge_a.datas,
    name='engram-mcp-bridge', console=True, debug=False, upx=False, strip=False,
    icon=['resource\\icon.ico'],
)
