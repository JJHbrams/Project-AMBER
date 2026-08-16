"""Update installer-owned paths in the Engram user config."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def _installer_path(value: str) -> str:
    """Store Windows paths in the portable form used by Engram config files."""
    return value.strip().replace("\\", "/")


def update_installer_paths(
    config_path: Path,
    *,
    db_dir: str,
    workdir: str,
) -> None:
    """Merge installer path selections while preserving all other YAML values."""
    data: object = {}
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise ValueError("user config root must be a mapping")

    db_config = data.get("db")
    if db_config is None:
        db_config = {}
        data["db"] = db_config
    if not isinstance(db_config, dict):
        raise ValueError("user config 'db' value must be a mapping")

    db_config["root_dir"] = _installer_path(db_dir)
    data["workdir"] = _installer_path(workdir)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_name(f"{config_path.name}.tmp")
    temporary_path.write_text(
        yaml.safe_dump(
            data,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(config_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args(argv)
    update_installer_paths(
        Path(args.config_path),
        db_dir=args.db_dir,
        workdir=args.workdir,
    )
    print(str(Path(args.config_path)))


if __name__ == "__main__":
    main()
