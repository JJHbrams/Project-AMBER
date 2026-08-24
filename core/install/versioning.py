"""Canonical four-part Engram version resolution.

Major.Minor.Patch is stored in the repository ``VERSION`` file.  Build is
resolved from a CI override first and the Git revision count otherwise.  A
frozen build carries a JSON snapshot so its identity never depends on the
machine where it is executed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


SNAPSHOT_NAME = "engram-version.json"
BUILD_ENVIRONMENT_KEYS = (
    "SEMVER4_BUILD",
    "GITHUB_RUN_NUMBER",
    "CI_PIPELINE_IID",
    "BUILD_NUMBER",
)
MAX_VERSION_PART = 65534


@dataclass(frozen=True)
class VersionInfo:
    base_version: str
    build: int
    version: str
    commit: str
    build_source: str

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_base_version(value: str) -> tuple[int, int, int]:
    parts = value.strip().split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError("VERSION must contain Major.Minor.Patch")
    numbers = tuple(int(part) for part in parts)
    if any(part < 0 or part > MAX_VERSION_PART for part in numbers):
        raise ValueError(f"VERSION parts must be between 0 and {MAX_VERSION_PART}")
    return numbers  # type: ignore[return-value]


def _run_git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def _read_snapshot(path: Path) -> VersionInfo:
    raw = json.loads(path.read_text(encoding="utf-8"))
    base = str(raw["base_version"])
    _parse_base_version(base)
    build = int(raw["build"])
    if build < 0 or build > MAX_VERSION_PART:
        raise ValueError(f"snapshot build must be between 0 and {MAX_VERSION_PART}")
    version = f"{base}.{build}"
    if raw.get("version") != version:
        raise ValueError("snapshot version does not match base_version and build")
    return VersionInfo(
        base_version=base,
        build=build,
        version=version,
        commit=str(raw.get("commit", "")),
        build_source=str(raw.get("build_source", "snapshot")),
    )


def frozen_snapshot_path() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / SNAPSHOT_NAME


def resolve_version(
    root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    snapshot_path: Path | None = None,
) -> VersionInfo:
    snapshot = snapshot_path if snapshot_path is not None else frozen_snapshot_path()
    if snapshot is not None:
        if not snapshot.is_file():
            raise FileNotFoundError(f"frozen version snapshot missing: {snapshot}")
        return _read_snapshot(snapshot)

    source_root = (root or repository_root()).resolve()
    base = ".".join(str(part) for part in _parse_base_version(
        (source_root / "VERSION").read_text(encoding="utf-8-sig")
    ))
    env = os.environ if environment is None else environment
    build = None
    build_source = ""
    for key in BUILD_ENVIRONMENT_KEYS:
        value = str(env.get(key, "")).strip()
        if not value:
            continue
        if not value.isdigit():
            raise ValueError(f"{key} must be an integer between 0 and {MAX_VERSION_PART}")
        build = int(value)
        if build > MAX_VERSION_PART:
            raise ValueError(f"{key} must be an integer between 0 and {MAX_VERSION_PART}")
        build_source = key
        break
    if build is None:
        count = _run_git(source_root, "rev-list", "--count", "HEAD")
        build = int(count) if count.isdigit() else 0
        build_source = "git" if count.isdigit() else "fallback"
    if build > MAX_VERSION_PART:
        build = MAX_VERSION_PART
    commit = _run_git(source_root, "rev-parse", "--short", "HEAD")
    return VersionInfo(base, build, f"{base}.{build}", commit, build_source)


def write_snapshot(path: Path, version: VersionInfo) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(version.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve Engram's four-part version")
    parser.add_argument("--root", type=Path, default=repository_root())
    parser.add_argument("--write-snapshot", type=Path)
    args = parser.parse_args(argv)
    version = resolve_version(args.root)
    if args.write_snapshot:
        write_snapshot(args.write_snapshot, version)
    print(json.dumps(version.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
