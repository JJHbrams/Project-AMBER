"""Direct provider executable discovery; never returns cmd/ps1 shims."""
from pathlib import Path
import os, shutil
def codex_executable() -> str:
    found=shutil.which('codex.exe' if os.name=='nt' else 'codex')
    if found and Path(found).suffix.lower() not in {'.cmd','.bat','.ps1'}: return found
    shim=shutil.which('codex.cmd') if os.name=='nt' else None
    if shim:
        candidates=list((Path(shim).parent/'node_modules/@openai/codex').glob('node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe'))
        if len(candidates)==1:return str(candidates[0])
    raise RuntimeError('codex_executable_unavailable')
