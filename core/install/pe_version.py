"""Restamp the VERSIONINFO resource of a PyInstaller exe without rebuilding it.

PyInstaller executables carry the archive as a PE overlay.  Resource updates
through ``BeginUpdateResource`` can drop or corrupt it, so the overlay is cut
off, the bare PE is restamped, and the overlay is re-appended before the PE
checksum is recomputed.  The result is written next to the target and moved
into place with ``os.replace`` so a hardlinked target is never written through.

Fields mirror ``engram-overlay.spec`` (both exes share one version resource).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

VERSION_STRINGS = {
    "CompanyName": "DRTECH",
    "FileDescription": "Engram Overlay",
    "InternalName": "engram-overlay",
    "OriginalFilename": "engram-overlay.exe",
    "ProductName": "Engram Overlay",
}


def parse_version(version: str) -> tuple[int, int, int, int]:
    parts = tuple(int(part) for part in version.split("."))
    if len(parts) != 4:
        raise ValueError(f"Invalid four-part version: {version}")
    return parts  # type: ignore[return-value]


def build_version_info(version: str):
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    numbers = parse_version(version)
    strings = [
        StringStruct("CompanyName", VERSION_STRINGS["CompanyName"]),
        StringStruct("FileDescription", VERSION_STRINGS["FileDescription"]),
        StringStruct("FileVersion", version),
        StringStruct("InternalName", VERSION_STRINGS["InternalName"]),
        StringStruct("OriginalFilename", VERSION_STRINGS["OriginalFilename"]),
        StringStruct("ProductName", VERSION_STRINGS["ProductName"]),
        StringStruct("ProductVersion", version),
    ]
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=numbers,
            prodvers=numbers,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([StringTable("040904B0", strings)]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )


def read_version(exe_path: Path | str) -> str | None:
    """FileVersion string of ``exe_path`` or ``None`` when unreadable."""
    try:
        import pefile

        with pefile.PE(str(exe_path), fast_load=True) as pe:
            pe.parse_data_directories(
                directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
            )
            for file_info in getattr(pe, "FileInfo", None) or []:
                for entry in file_info:
                    for table in getattr(entry, "StringTable", None) or []:
                        value = table.entries.get(b"FileVersion")
                        if value:
                            return value.decode("utf-8", "replace")
    except Exception:
        return None
    return None


def restamp(exe_path: Path | str, version: str) -> None:
    """Rewrite the version resource of ``exe_path`` to ``version`` atomically."""
    import pefile
    from PyInstaller import config as pyi_config
    from PyInstaller.utils.win32 import versioninfo, winutils

    target = Path(exe_path)
    info = build_version_info(version)
    data = target.read_bytes()

    with pefile.PE(data=data, fast_load=True) as pe:
        cut = pe.get_overlay_data_start_offset()
    bare, overlay = (data[:cut], data[cut:]) if cut else (data, b"")

    workdir = tempfile.mkdtemp(prefix="engram-pe-version-")
    temp = target.with_name(f".{target.name}.{os.getpid()}.restamp")
    previous = dict(pyi_config.CONF)
    try:
        pyi_config.CONF["workpath"] = workdir
        temp.write_bytes(bare)
        versioninfo.write_version_info_to_executable(str(temp), info)
        if overlay:
            with temp.open("ab") as stream:
                stream.write(overlay)
        winutils.update_exe_pe_checksum(str(temp))
        os.replace(temp, target)
    finally:
        pyi_config.CONF.clear()
        pyi_config.CONF.update(previous)
        try:
            temp.unlink()
        except OSError:
            pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass
