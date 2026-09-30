"""engram-overlay.exe 빌드용 엔트리포인트 (얇은 스텁).

이 스크립트는 PyInstaller PKG 에 구워지므로 수정하면 전체 재빌드가 필요하다.
모든 로직은 core/entrypoint.py 에 있다.
"""

from core.entrypoint import run

run()
