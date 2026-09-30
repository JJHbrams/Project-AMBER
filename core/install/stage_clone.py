"""Clone an artifact tree with hardlinks (real copies for selected files)."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def clone_tree(src: Path, dst: Path, copy_names: set[str] | None = None) -> dict[str, int]:
    """Hardlink every file of ``src`` into ``dst``.

    Top-level files named in ``copy_names`` are real copies so they can be
    rewritten (PE restamp / replacement) without touching the live artifact.
    Files that cannot be hardlinked (e.g. cross-volume) fall back to a copy.
    """
    src = Path(src)
    dst = Path(dst)
    copy_names = {name.lower() for name in (copy_names or set())}
    stats = {"linked": 0, "copied": 0, "dirs": 0}
    dst.mkdir(parents=True, exist_ok=False)

    def walk(source: Path, target: Path, top: bool) -> None:
        with os.scandir(source) as entries:
            for entry in entries:
                destination = target / entry.name
                if entry.is_dir(follow_symlinks=False):
                    destination.mkdir()
                    stats["dirs"] += 1
                    walk(Path(entry.path), destination, False)
                    continue
                if top and entry.name.lower() in copy_names:
                    shutil.copy2(entry.path, destination)
                    stats["copied"] += 1
                    continue
                try:
                    os.link(entry.path, destination)
                    stats["linked"] += 1
                except OSError:
                    shutil.copy2(entry.path, destination)
                    stats["copied"] += 1

    walk(src, dst, True)
    return stats


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--dst", required=True, type=Path)
    parser.add_argument("--copy", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        print(json.dumps(clone_tree(args.src, args.dst, set(args.copy))))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
