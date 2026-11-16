"""Native Mnema character using the established Engram icon event semantics."""
from __future__ import annotations

from pathlib import Path

from .native_engram_icon import ENGRAM_ICON_STATE_STRIPS, ICON_CLIPS, ICON_OPTIONS
from .mnema_mapping import CATEGORIES, HINTS, LIFECYCLE, ONESHOTS, load_mapping
from .native_bolttagu import Bolttagu2dView


ASSET_DIR = Path(__file__).resolve().parents[1] / "resource" / "character" / "mnema"
SPRITE_PREFIX = "mnema-"
MNEMA_STATE_STRIPS = ENGRAM_ICON_STATE_STRIPS


class MnemaView(Bolttagu2dView):
    def __init__(self, *, mapping_path: Path | None = None, log=None, **kwargs) -> None:
        mapping = load_mapping(mapping_path, log=log)
        super().__init__(
            mapping_path=None,
            asset_dir=ASSET_DIR,
            sprite_prefix=SPRITE_PREFIX,
            required_options=ICON_OPTIONS,
            clips=ICON_CLIPS,
            hints=mapping.hints,
            categories=mapping.categories,
            oneshots=mapping.oneshots,
            lifecycle=mapping.lifecycle,
            **kwargs,
        )
