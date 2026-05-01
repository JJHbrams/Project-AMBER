"""
scripts/engram_dashboard.py — 스탠드얼론 런처

overlay.exe 기동 시 자동 실행되거나, 직접 실행할 수 있는 얇은 진입점.
실제 대시보드 로직은 core/dashboard/app.py 에 있습니다.

Usage:
    conda run -n intel_engram streamlit run scripts/engram_dashboard.py
"""
import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

runpy.run_path(
    str(_ROOT / "core" / "dashboard" / "app.py"),
    run_name="__main__",
)
