"""Safe, manifest-based resolver for optional character asset packs."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml
from PIL import Image

from overlay.config import resolve_editable_overlay_path, resolve_path


USER_CHARACTER_SETS_DIR = Path.home() / ".engram" / "character" / "sets"
_BUNDLED_SETS_REL = "resource/character/sets"
USER_REACTION_PACKS_DIR = Path.home() / ".engram" / "character" / "reactions"
_BUNDLED_REACTIONS_REL = "resource/character/reactions"
_BUNDLED_STATIC_REL = "resource/character/static"
_BUNDLED_SEQUENCES_REL = "resource/character/sequences"


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


@dataclass(frozen=True)
class TimelinePosition:
    """Pure resolved timeline metadata; rendering remains host-owned."""

    frames: tuple[int, ...]
    frame_index: int
    elapsed_in_frame_ms: int
    holding: bool
    completed: bool
    cycle: int


def _lcg_state(seed: object, cycle: int) -> int:
    state = int.from_bytes(hashlib.sha256(str(seed).encode("utf-8")).digest()[:4], "big")
    for _ in range(max(0, int(cycle)) + 1):
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
    return state


def _hold_bounds(hold_ms: object) -> tuple[int, int] | None:
    if not isinstance(hold_ms, (tuple, list)) or len(hold_ms) != 2:
        return None
    low, high = hold_ms
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (low, high)):
        return None
    low, high = max(0, low), max(0, high)
    return (low, high) if low <= high else (high, low)


def deterministic_hold_ms(hold_ms: object, seed: object, cycle: int = 0) -> int:
    """Resolve an inclusive hold range using the per-cycle LCG draw."""
    if isinstance(hold_ms, int) and not isinstance(hold_ms, bool):
        return max(0, hold_ms)
    bounds = _hold_bounds(hold_ms)
    if bounds is None:
        return 0
    low, high = bounds
    return low + _lcg_state(seed, cycle) % (high - low + 1)


def _timeline_durations(spec: dict) -> tuple[int, ...]:
    frames = tuple(spec.get("frames") or (0,))
    default_duration = max(1, int(spec.get("frame_ms", 600)))
    declared = spec.get("durations_ms")
    values = tuple(declared) if isinstance(declared, (tuple, list)) and len(declared) == len(frames) else (default_duration,) * len(frames)
    return tuple(max(1, int(value)) for value in values)


def timeline_nominal_total_ms(spec: dict) -> int:
    """Return one layer's nominal cycle duration using a ranged hold midpoint."""
    durations = _timeline_durations(spec)
    hold = spec.get("hold_ms")
    if isinstance(hold, (tuple, list)) and len(hold) == 2:
        low, high = sorted((max(0, int(hold[0])), max(0, int(hold[1]))))
        first = (low + high) // 2
    elif isinstance(hold, int) and not isinstance(hold, bool):
        first = max(0, hold)
    else:
        first = durations[0]
    return max(1, first + sum(durations[1:]))


def resolve_timeline_position(spec: dict, elapsed_ms: float, *, seed: object = 0) -> TimelinePosition:
    """Resolve duration boundaries, loop holds, and non-loop completion."""
    frames = tuple(spec.get("frames") or (0,))
    durations = _timeline_durations(spec)
    has_hold = "hold_ms" in spec
    loop = bool(spec.get("loop", spec.get("selection") != "sequence_once"))
    elapsed = max(0, int(elapsed_ms))
    cycle = 0
    position = elapsed
    variable_hold = has_hold and isinstance(spec["hold_ms"], (tuple, list))
    if loop and not variable_hold:
        first_duration = deterministic_hold_ms(spec["hold_ms"], seed, cycle) if has_hold else durations[0]
        cycle_durations = (first_duration,) + durations[1:]
        cycle_ms = max(1, sum(cycle_durations))
        cycle, position = divmod(elapsed, cycle_ms)
    else:
        hold_bounds = _hold_bounds(spec["hold_ms"]) if variable_hold else None
        hold_state = int.from_bytes(hashlib.sha256(str(seed).encode("utf-8")).digest()[:4], "big")
        while True:
            if hold_bounds is not None:
                hold_state = (1664525 * hold_state + 1013904223) & 0xFFFFFFFF
                first_duration = hold_bounds[0] + hold_state % (hold_bounds[1] - hold_bounds[0] + 1)
            else:
                first_duration = deterministic_hold_ms(spec["hold_ms"], seed, cycle) if has_hold else durations[0]
            cycle_durations = (first_duration,) + durations[1:]
            cycle_ms = max(1, sum(cycle_durations))
            if not loop:
                if position >= cycle_ms:
                    return TimelinePosition((frames[-1],), len(frames) - 1, durations[-1], True, True, 0)
                break
            if position < cycle_ms:
                break
            position -= cycle_ms
            cycle += 1
    cursor = 0
    for index, duration in enumerate(cycle_durations):
        boundary = cursor + duration
        if position < boundary:
            return TimelinePosition((frames[index],), index, position - cursor, has_hold and index == 0, False, cycle)
        cursor = boundary
    return TimelinePosition((frames[-1],), len(frames) - 1, durations[-1], True, not loop, cycle)


def resolve_layered_timeline(spec: dict, elapsed_ms: float, *, seed: object = 0) -> TimelinePosition:
    """Resolve base plus optional declared layers as composition metadata."""
    base = resolve_timeline_position(spec, elapsed_ms, seed=f"{seed}:base")
    layers = [base.frames[0]]
    timeline_specs = (spec,) + tuple(spec.get("layers") or ())
    option_completes = any(not bool(layer.get("loop", layer.get("selection") != "sequence_once")) for layer in timeline_specs)
    completed = option_completes and max(0, int(elapsed_ms)) >= max(timeline_nominal_total_ms(layer) for layer in timeline_specs)
    holding = base.holding
    for index, layer in enumerate(spec.get("layers") or ()):
        position = resolve_timeline_position(layer, elapsed_ms, seed=f"{seed}:layer:{index}")
        layers.append(position.frames[0])
        holding = holding or position.holding
    return TimelinePosition(tuple(layers), base.frame_index, base.elapsed_in_frame_ms, holding, completed, base.cycle)


def rotation_value(options: tuple[int, ...], step: int, *, seed: object = 0) -> int:
    """Return a deterministic rotation whose adjacent values never repeat."""
    if not options:
        raise ValueError("rotation requires at least one option")
    if len(options) == 1:
        return options[0]
    offset = int.from_bytes(hashlib.sha256(str(seed).encode("utf-8")).digest()[:8], "big") % len(options)
    return options[(offset + max(0, int(step))) % len(options)]


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


def resolve_bundled_character_source(value: object, source_mode: str) -> Path | None:
    """Resolve canonical and legacy *bundled* character references.

    Absolute paths are deliberately not remapped: a missing user-selected absolute
    path must stay missing instead of unexpectedly selecting a bundled asset.  The
    compatibility aliases below are limited to safe logical ids and historical
    ``resource/character`` relative paths shipped by Engram.
    """
    raw = str(value or "").strip()
    candidate = Path(raw).expanduser()
    if not raw:
        return None

    if candidate.is_absolute():
        # Settings historically persisted an absolute path to the checkout's
        # old flat bundled layout.  Remap only that exact current bundled root;
        # never redirect an arbitrary missing user path that happens to share a
        # filename.
        bundled_root = resolve_editable_overlay_path("resource/character")
        try:
            parent_matches = candidate.parent.resolve() == bundled_root.resolve()
        except OSError:
            parent_matches = False
        if not parent_matches:
            return None
        normalized = candidate.name
    else:
        normalized = raw.replace("\\", "/").removeprefix("./")

    direct = resolve_editable_overlay_path(normalized)
    if normalized.startswith("resource/character/"):
        if source_mode == "static" and direct.is_file() and direct.suffix.lower() == ".png":
            return direct
        if source_mode == "sequence" and direct.is_dir():
            return direct

    logical = normalized
    prefix = "resource/character/"
    if logical.startswith(prefix):
        logical = logical[len(prefix):]
    if source_mode == "static" and logical.endswith(".png") and "/" not in logical:
        logical = logical[:-4]
    if source_mode == "sequence" and "/" in logical:
        return None
    safe_id = _safe_set_id(logical)
    if safe_id is None:
        return None

    if source_mode == "static":
        static_image = resolve_editable_overlay_path(f"{_BUNDLED_STATIC_REL}/{safe_id}.png")
        if static_image.is_file():
            return static_image
        bundled_set = resolve_character_set(safe_id)
        if bundled_set.source == "bundled":
            return bundled_set.base_image
    elif source_mode == "sequence":
        sequence = resolve_editable_overlay_path(f"{_BUNDLED_SEQUENCES_REL}/{safe_id}")
        if sequence.is_dir():
            return sequence
    return None


def resolve_bundled_reaction_sheet(value: object) -> Path | None:
    """Map the one removed bundled Engram grid path to its canonical pack sheet."""
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    legacy_relative = "resource/character/engram_set/engram_states.png"
    if candidate.is_absolute():
        bundled_root = resolve_editable_overlay_path("resource/character")
        legacy_root = bundled_root / "engram_set"
        try:
            matches = candidate.parent.resolve() == legacy_root.resolve() and candidate.name.lower() == "engram_states.png"
        except OSError:
            matches = False
        if not matches:
            return None
    elif raw.replace("\\", "/").removeprefix("./") != legacy_relative:
        return None
    pack = resolve_reaction_pack("engram")
    return pack.sprite_sheet if pack.source == "bundled" else None


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
        allowed_selection = {"random", "rotation", "sequence", "sequence_once", "fixed", "shuffle"}

        def normalize_timeline(item: dict, *, require_frames: bool = True) -> dict | None:
            frames = item.get("frames")
            if require_frames and (not isinstance(frames, list) or not frames):
                return None
            if not isinstance(frames, list) or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < columns * rows for v in frames):
                return None
            timing = _positive_int(item.get("frame_ms", item.get("dwell_ms", 600)))
            if timing is None:
                return None
            normalized: dict = {"frames": tuple(frames), "frame_ms": timing}
            if "durations_ms" in item:
                durations = item["durations_ms"]
                if not isinstance(durations, list) or len(durations) != len(frames):
                    return None
                parsed = tuple(_positive_int(value) for value in durations)
                if any(value is None or value > 60_000 for value in parsed):
                    return None
                normalized["durations_ms"] = parsed
            if "loop" in item:
                if not isinstance(item["loop"], bool):
                    return None
                normalized["loop"] = item["loop"]
            if "hold_ms" in item:
                hold = item["hold_ms"]
                if isinstance(hold, int) and not isinstance(hold, bool) and 0 <= hold <= 60_000:
                    normalized["hold_ms"] = hold
                elif (isinstance(hold, list) and len(hold) == 2
                      and all(isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 60_000 for value in hold)):
                    normalized["hold_ms"] = tuple(hold)
                else:
                    return None
            return normalized

        for name, item in states_raw.items():
            if not isinstance(name, str) or not isinstance(item, dict):
                return None
            timeline = normalize_timeline(item)
            if timeline is None:
                return None
            frames = item["frames"]
            selection = item.get("selection", "fixed" if len(frames) == 1 else "random")
            transform = normalize_sprite_transform(item.get("transform", "none"))
            vfx = normalize_sprite_vfx(item.get("vfx", "none"))
            if selection not in allowed_selection or transform is None or vfx is None:
                return None
            if selection == "rotation" and any(key in item for key in ("durations_ms", "loop", "hold_ms", "layers")):
                return None
            layers: tuple[dict, ...] = ()
            if "layers" in item:
                if not isinstance(item["layers"], list) or not item["layers"]:
                    return None
                parsed_layers = []
                for layer in item["layers"]:
                    parsed = normalize_timeline(layer) if isinstance(layer, dict) else None
                    if parsed is None:
                        return None
                    parsed_layers.append(parsed)
                layers = tuple(parsed_layers)
            timelines = (timeline,) + layers
            completing_timelines = tuple(
                part for part in timelines
                if not bool(part.get("loop", part.get("selection") != "sequence_once"))
            )
            default_dwell = max(
                timeline_nominal_total_ms(part)
                for part in (timelines if completing_timelines else (timeline,))
            )
            dwell = _positive_int(item.get("dwell_ms", default_dwell))
            if dwell is None:
                return None
            normalized = {"frames": tuple(frames), "selection": selection, "transform": transform, "vfx": vfx,
                          "frame_ms": timeline["frame_ms"], "dwell_ms": dwell}
            normalized.update({key: value for key, value in timeline.items() if key not in {"frames", "frame_ms"}})
            if layers:
                normalized["layers"] = layers
            states[name] = normalized
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
    path = resolve_bundled_reaction_sheet(sprite_sheet) or Path(str(sprite_sheet or "")).expanduser()
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
