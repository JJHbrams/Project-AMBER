"""Update installer-owned paths in the Engram user config."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def _installer_path(value: str) -> str:
    """Store Windows paths in the portable form used by Engram config files."""
    return value.strip().replace("\\", "/")


def preserve_legacy_character_source_mode(data: dict) -> bool:
    """Infer the pre-source_mode character choice from an explicit user path.

    Older overlay.user.yaml files stored only ``overlay.character.name``.  Once
    the project default became ``sprite_grid``, a normal deep merge made that
    new default override the user's existing custom image or frame directory.
    Add an override only for an explicit non-default legacy selection; a user
    who never customized the character continues to receive the new default.
    """
    overlay = data.get("overlay")
    if not isinstance(overlay, dict):
        return False
    character = overlay.get("character")
    if not isinstance(character, dict) or "source_mode" in character:
        return False

    raw_name = str(character.get("name") or "").strip()
    if not raw_name or raw_name.casefold() == "engram":
        return False

    candidate = Path(raw_name).expanduser()
    normalized = raw_name.replace("\\", "/").rstrip("/")
    is_sequence = candidate.is_dir() or (
        "/" in normalized and Path(normalized).suffix.casefold() != ".png"
    )
    character["source_mode"] = "sequence" if is_sequence else "static"
    return True


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


def update_overlay_installer_config(config_path: Path, *, provider: str, mcp_port: int, ollama_model: str = "") -> None:
    """Merge installer-owned overlay settings while preserving unrelated values."""
    data = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("overlay user config root must be a mapping")
    if data.get("cli") is not None and not isinstance(data.get("cli"), dict):
        raise ValueError("overlay user config 'cli' value must be a mapping")
    if data.get("mcp") is not None and not isinstance(data.get("mcp"), dict):
        raise ValueError("overlay user config 'mcp' value must be a mapping")
    preserve_legacy_character_source_mode(data)
    cli = data.get("cli") if isinstance(data.get("cli"), dict) else {}
    cli["provider"] = provider
    if ollama_model:
        cli["ollama_model"] = ollama_model
    data["cli"] = cli
    mcp = data.get("mcp") if isinstance(data.get("mcp"), dict) else {}
    mcp["http_port"] = int(mcp_port)
    data["mcp"] = mcp
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_name(f"{config_path.name}.tmp")
    temporary_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    temporary_path.replace(config_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-path", required=True)
    parser.add_argument("--db-dir")
    parser.add_argument("--workdir")
    parser.add_argument("--overlay-provider")
    parser.add_argument("--overlay-mcp-port", type=int)
    parser.add_argument("--overlay-ollama-model", default="")
    args = parser.parse_args(argv)
    if args.overlay_provider is not None:
        if args.overlay_mcp_port is None:
            parser.error("--overlay-mcp-port is required with --overlay-provider")
        update_overlay_installer_config(Path(args.config_path), provider=args.overlay_provider, mcp_port=args.overlay_mcp_port, ollama_model=args.overlay_ollama_model)
    else:
        if not args.db_dir or not args.workdir:
            parser.error("--db-dir and --workdir are required")
        update_installer_paths(Path(args.config_path), db_dir=args.db_dir, workdir=args.workdir)
    print(str(Path(args.config_path)))


if __name__ == "__main__":
    main()
