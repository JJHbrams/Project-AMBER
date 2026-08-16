"""Frozen bootstrap for Tcl/Tk when PyInstaller's standard probe is unavailable."""

import os
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    tcl_data = bundle_root / "_tcl_data"
    tk_data = bundle_root / "_tk_data"
    if tcl_data.is_dir():
        os.environ["TCL_LIBRARY"] = str(tcl_data)
    if tk_data.is_dir():
        os.environ["TK_LIBRARY"] = str(tk_data)
