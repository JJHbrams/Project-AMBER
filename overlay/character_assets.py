"""Safe, manifest-based resolver for optional character asset packs."""

from dataclasses import dataclass
from pathlib import Path

import yaml
from PIL import Image

from overlay.config import resolve_editable_overlay_path, resolve_path


USER_CHARACTER_SETS_DIR = Path.home() / ".engram" / "character" / "sets"
_BUNDLED_SETS_REL = "resource/character/sets"
USER_REACTION_PACKS_DIR = Path.home() / ".engram" / "character" / "reactions"
_BUNDLED_REACTIONS_REL = "resource/character/reactions"


@dataclass(frozen=True)
class CharacterEffectAsset:
    path: Path
    thickness_px: int


@dataclass(frozen=True)
class CharacterSetResolution:
    source: str = "disabled"
    base_image: Path | None = None
    idle: CharacterEffectAsset | None = None
    click: CharacterEffectAsset | None = None


@dataclass(frozen=True)
class ReactionPackResolution:
    source: str = "disabled"
    sprite_sheet: Path | None = None
    chroma_key: str = "#00FF00"
    columns: int = 0
    rows: int = 0
    cell_width: int = 0
    cell_height: int = 0
    mapping: dict[str, int] | None = None
    states: dict[str, dict] | None = None
    crop_y_offset_px: int = 0


_BUILTIN_STATE_TEMPLATE = {
    "default": {"frames": (18,), "selection": "fixed", "transform": "none", "vfx": "none", "frame_ms": 900, "dwell_ms": 900},
    "idle": {"frames": (18, 19, 20, 21, 22), "selection": "shuffle", "transform": "none", "vfx": "twinkle", "frame_ms": 2400, "dwell_ms": 2400},
    "hover": {"frames": (17,), "selection": "fixed", "transform": "hflip_squash", "vfx": "none", "frame_ms": 320, "dwell_ms": 320},
    "click": {"frames": (9, 10, 11), "selection": "random", "transform": "none", "vfx": "sparkle_burst", "frame_ms": 1000, "dwell_ms": 1000},
    "input": {"frames": (12, 14), "selection": "random", "transform": "none", "vfx": "none", "frame_ms": 1600, "dwell_ms": 1600},
    "generating": {"frames": (14,), "selection": "fixed", "transform": "none", "vfx": "none", "frame_ms": 700, "dwell_ms": 700},
    "search": {"frames": (0, 1, 2, 3, 4), "selection": "sequence", "transform": "none", "vfx": "none", "frame_ms": 260, "dwell_ms": 260},
    "thought": {"frames": (5, 7, 13), "selection": "random", "transform": "none", "vfx": "none", "frame_ms": 480, "dwell_ms": 480},
    "memory": {"frames": (6,), "selection": "fixed", "transform": "none", "vfx": "none", "frame_ms": 700, "dwell_ms": 700},
    "success": {"frames": (16,), "selection": "fixed", "transform": "none", "vfx": "none", "frame_ms": 2400, "dwell_ms": 2400},
    "provider_error": {"frames": (8,), "selection": "fixed", "transform": "none", "vfx": "none", "frame_ms": 650, "dwell_ms": 1800},
    "error": {"frames": (15,), "selection": "fixed", "transform": "none", "vfx": "none", "frame_ms": 650, "dwell_ms": 1800},
}

# Manifest values describe rendered effects, rather than the event names which
# originally happened to use them. Keep the first release's short names as
# input aliases so user-authored packs remain valid when they are reloaded.
_TRANSFORM_ALIASES = {
    "none": "none",
    "idle": "breathe_mirror",
    "breathe_mirror": "breathe_mirror",
    "hover": "hflip_squash",
    "hover_flip_squash": "hflip_squash",
    "alternating_mirror_squash": "hflip_squash",
    "hflip_squash": "hflip_squash",
    # The old click transform did not render any transform at all.
    "click": "none",
}
_VFX_ALIASES = {
    "none": "none",
    "idle": "twinkle",
    "twinkle": "twinkle",
    "click": "sparkle_burst",
    "sparkle": "sparkle_burst",
    "sparkle_burst": "sparkle_burst",
}


def normalize_sprite_transform(value: object) -> str | None:
    """Return a canonical transform name, accepting legacy manifest aliases."""
    return _TRANSFORM_ALIASES.get(str(value or "").strip().lower())


def normalize_sprite_vfx(value: object) -> str | None:
    """Return a canonical VFX name, accepting legacy manifest aliases."""
    return _VFX_ALIASES.get(str(value or "").strip().lower())


def _safe_cell_zero_template() -> dict[str, dict]:
    return {
        name: {**spec, "frames": (0,)}
        for name, spec in _BUILTIN_STATE_TEMPLATE.items()
    }


def _inline_state_template(cell_count: int) -> dict[str, dict]:
    required_indices = [index for spec in _BUILTIN_STATE_TEMPLATE.values() for index in spec["frames"]]
    return _BUILTIN_STATE_TEMPLATE if all(index < cell_count for index in required_indices) else _safe_cell_zero_template()


def _grid_matches_sheet(path: Path, columns: int, rows: int, cell_width: int, cell_height: int) -> bool:
    try:
        with Image.open(path) as image:
            return image.size == (columns * cell_width, rows * cell_height)
    except (OSError, ValueError):
        return False


def _clamp_thickness(value: object, default: int) -> int:
    try:
        return max(1, min(6, int(value)))
    except (TypeError, ValueError):
        return default


def _safe_set_id(value: object) -> str | None:
    set_id = str(value or "").strip()
    if not set_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in set_id):
        return None
    return set_id


def _safe_pack_asset(root: Path, value: object) -> Path | None:
    raw = str(value or "").strip()
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or ".." in candidate.parts:
        return None
    path = root / candidate
    try:
        if path.is_file() and path.resolve().is_relative_to(root.resolve()):
            return path
    except (OSError, ValueError):
        return None
    return None


def _read_manifest(root: Path, source: str) -> CharacterSetResolution | None:
    manifest_path = root / "manifest.yaml"
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return None

    base_image = _safe_pack_asset(root, raw.get("character"))
    if base_image is None:
        return None

    effects = raw.get("effects")
    effects = effects if isinstance(effects, dict) else {}

    def effect(name: str, default_thickness: int) -> CharacterEffectAsset | None:
        data = effects.get(name)
        if not isinstance(data, dict):
            return None
        path = _safe_pack_asset(root, data.get("asset"))
        return CharacterEffectAsset(path, _clamp_thickness(data.get("thickness_px"), default_thickness)) if path else None

    return CharacterSetResolution(source=source, base_image=base_image, idle=effect("idle", 2), click=effect("click", 3))


def resolve_character_set(set_id: object) -> CharacterSetResolution:
    """Resolve a user pack before its bundled counterpart; invalid packs are non-fatal."""
    safe_id = _safe_set_id(set_id)
    if safe_id is None:
        return CharacterSetResolution()
    roots = (
        (USER_CHARACTER_SETS_DIR / safe_id, "user"),
        (resolve_editable_overlay_path(f"{_BUNDLED_SETS_REL}/{safe_id}"), "bundled"),
    )
    for root, source in roots:
        result = _read_manifest(root, source)
        if result is not None:
            return result
    return CharacterSetResolution()


def _valid_chroma_key(value: object) -> str | None:
    text = str(value or "").strip()
    if len(text) != 7 or not text.startswith("#"):
        return None
    try:
        int(text[1:], 16)
    except ValueError:
        return None
    return text.upper()


def _positive_int(value: object) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _valid_crop_y_offset(value: object, cell_height: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        offset = int(value)
    except (TypeError, ValueError):
        return None
    return offset if 0 <= offset < cell_height else None


def _read_reaction_manifest(root: Path, source: str, expected_id: str) -> ReactionPackResolution | None:
    try:
        raw = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or _safe_set_id(raw.get("id")) != expected_id:
        return None
    sheet = _safe_pack_asset(root, raw.get("sprite_sheet"))
    grid = raw.get("grid") if isinstance(raw.get("grid"), dict) else {}
    columns, rows = _positive_int(grid.get("columns")), _positive_int(grid.get("rows"))
    cell_width, cell_height = _positive_int(grid.get("cell_width")), _positive_int(grid.get("cell_height"))
    chroma_key = _valid_chroma_key(raw.get("chroma_key"))
    crop_y_offset_px = _valid_crop_y_offset(raw.get("crop_y_offset_px", 0), cell_height or 0)
    mapping_raw = raw.get("mapping", {})
    if None in (sheet, columns, rows, cell_width, cell_height, chroma_key, crop_y_offset_px) or not isinstance(mapping_raw, dict):
        return None
    mapping: dict[str, int] = {}
    for state, index in mapping_raw.items():
        if not isinstance(state, str) or isinstance(index, bool):
            return None
        try:
            numeric_index = int(index)
        except (TypeError, ValueError):
            return None
        if not 0 <= numeric_index < columns * rows:
            return None
        mapping[state] = numeric_index
    states_raw = raw.get("states")
    states: dict[str, dict] = {}
    if states_raw is not None:
        if not isinstance(states_raw, dict):
            return None
        allowed_selection = {"random", "sequence", "sequence_once", "fixed", "shuffle"}
        for name, item in states_raw.items():
            if not isinstance(name, str) or not isinstance(item, dict):
                return None
            frames = item.get("frames")
            if not isinstance(frames, list) or not frames or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < columns * rows for v in frames):
                return None
            selection = item.get("selection", "fixed" if len(frames) == 1 else "random")
            transform = normalize_sprite_transform(item.get("transform", "none"))
            vfx = normalize_sprite_vfx(item.get("vfx", "none"))
            if selection not in allowed_selection or transform is None or vfx is None:
                return None
            timing = _positive_int(item.get("frame_ms", item.get("dwell_ms", 600)))
            dwell = _positive_int(item.get("dwell_ms", timing))
            if timing is None or dwell is None:
                return None
            states[name] = {"frames": tuple(frames), "selection": selection, "transform": transform, "vfx": vfx,
                            "frame_ms": timing, "dwell_ms": dwell}
    if not mapping and not states:
        return None
    return ReactionPackResolution(source, sheet, chroma_key, columns, rows, cell_width, cell_height, mapping, states, crop_y_offset_px)


def resolve_reaction_pack(pack_id: object) -> ReactionPackResolution:
    """Resolve a valid user reaction pack before its bundled counterpart."""
    safe_id = _safe_set_id(pack_id)
    if safe_id is None:
        return ReactionPackResolution()
    roots = (
        (USER_REACTION_PACKS_DIR / safe_id, "user"),
        (resolve_editable_overlay_path(f"{_BUNDLED_REACTIONS_REL}/{safe_id}"), "bundled"),
    )
    for root, source in roots:
        result = _read_reaction_manifest(root, source, safe_id)
        if result is not None:
            return result
    return ReactionPackResolution()


def inline_reaction_pack(sprite_sheet: object, grid: object, chroma_key: object, crop_y_offset_px: object = 0) -> ReactionPackResolution:
    """Validate an explicitly selected local sprite grid (the settings UI path)."""
    path = Path(str(sprite_sheet or "")).expanduser()
    if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".png" or not isinstance(grid, dict):
        return ReactionPackResolution()
    columns, rows = _positive_int(grid.get("columns")), _positive_int(grid.get("rows"))
    cell_width, cell_height = _positive_int(grid.get("cell_width")), _positive_int(grid.get("cell_height"))
    key = _valid_chroma_key(chroma_key)
    offset = _valid_crop_y_offset(crop_y_offset_px, cell_height or 0)
    if None in (columns, rows, cell_width, cell_height, key, offset):
        return ReactionPackResolution()
    if not _grid_matches_sheet(path, columns, rows, cell_width, cell_height):
        return ReactionPackResolution()
    states = _inline_state_template(columns * rows)
    return ReactionPackResolution("inline", path, key, columns, rows, cell_width, cell_height, {}, states, offset)


def resolve_legacy_asset(value: object) -> Path | None:
    """Resolve the existing effects config paths without applying pack path rules."""
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    candidates = (candidate,) if candidate.is_absolute() else (Path.home() / ".engram" / candidate, resolve_path(raw))
    for path in candidates:
        try:
            if path.is_file() and path.suffix.lower() == ".png":
                return path
        except OSError:
            continue
    return None
