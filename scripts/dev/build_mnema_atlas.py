"""Build the fixed-cell Mnema native overlay atlas from the approved raw strips."""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "docs" / "character" / "mnema-raw"
OUT_DIR = ROOT / "resource" / "character" / "mnema"
CALIBRATION_PATH = ROOT / "docs" / "character" / "mnema-calibration.png"
CELL = (270, 302)
CENTER_X = 135
FEET_Y = 270
STRIP_SCALE = 0.32
ALERT_SCALE = 0.215
STATES = (
    "idle", "waiting", "listening", "speaking", "searching", "wondering", "writing",
    "tool_use", "executing", "success", "error", "enter", "exit",
)


def _bbox(image: Image.Image, threshold: int = 20) -> tuple[int, int, int, int] | None:
    alpha = image.getchannel("A").point(lambda value: 255 if value > threshold else 0)
    return alpha.getbbox()


def _foot_y(image: Image.Image) -> int:
    """Use the opaque boot baseline; raw effect wisps can extend lower."""
    bbox = _bbox(image)
    if bbox is None:
        raise ValueError("empty Mnema frame")
    # The source strips consistently place the boots in their final opaque rows.
    # Only exit contains a low transient effect, so cap its baseline at the prior boot row.
    return min(bbox[3] - 1, 783)


def _place_scaled(image: Image.Image, scale: float, foot_y: int) -> Image.Image:
    width = round(image.width * scale)
    height = round(image.height * scale)
    scaled = image.resize((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", CELL)
    x = round(CENTER_X - width / 2)
    y = FEET_Y - round(foot_y * scale)
    canvas.alpha_composite(scaled, (x, y))
    return canvas


def _steam_sheet() -> Image.Image:
    sheet = Image.new("RGBA", (CELL[0] * 24, CELL[1]))
    for index in range(24):
        cell = Image.new("RGBA", CELL)
        draw = ImageDraw.Draw(cell)
        phase = index / 23
        for curl, base_x in enumerate((111, 128, 145)):
            y = 230 - round(phase * 40) - curl * 7
            alpha = max(0, round(75 * (1 - phase) * (1 - curl * 0.15)))
            draw.arc((base_x - 5, y - 10, base_x + 7, y + 10), 145, 330, fill=(146, 255, 220, alpha), width=2)
        sheet.alpha_composite(cell, (index * CELL[0], 0))
    return sheet


def _head_and_cheek_width(image: Image.Image) -> tuple[int, int]:
    """Report stable visual widths from alpha rows near head and cheek center."""
    alpha = image.getchannel("A")
    def width_at(y: int) -> int:
        runs = [x for x in range(image.width) if alpha.getpixel((x, y)) > 20]
        return (max(runs) - min(runs) + 1) if runs else 0
    return width_at(84), width_at(113)


def _contact_sheet(frames: dict[str, tuple[Image.Image, ...]]) -> None:
    names = [*STATES, "alert"]
    cols, row_height = 5, 350
    sheet = Image.new("RGBA", (cols * 3 * CELL[0], 3 * row_height), (246, 249, 248, 255))
    draw = ImageDraw.Draw(sheet)
    for index, name in enumerate(names):
        col, row = index % cols, index // cols
        left, top = col * 3 * CELL[0], row * row_height
        draw.text((left + 8, top + 8), name, fill=(25, 45, 40, 255))
        draw.line((left, top + 290, left + 3 * CELL[0], top + 290), fill=(70, 205, 170, 190), width=1)
        for cell_index, frame in enumerate(frames[name]):
            sheet.alpha_composite(frame, (left + cell_index * CELL[0], top + 18))
    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    sheet.convert("RGB").save(CALIBRATION_PATH)


def build() -> dict[str, object]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rendered: dict[str, tuple[Image.Image, ...]] = {}
    report: dict[str, object] = {"cell": list(CELL), "center_x": CENTER_X, "feet_y": FEET_Y, "states": {}}
    for state in STATES:
        with Image.open(RAW_DIR / f"{state}.png") as raw:
            source = raw.convert("RGBA")
        if source.size != (1983, 793):
            raise ValueError(f"{state}.png must be 1983x793, got {source.size}")
        frames = []
        metrics = []
        for index in range(3):
            source_frame = source.crop((index * 661, 0, (index + 1) * 661, 793))
            frame = _place_scaled(source_frame, STRIP_SCALE, _foot_y(source_frame))
            frames.append(frame)
            bbox = _bbox(frame)
            head_width, cheek_width = _head_and_cheek_width(frame)
            metrics.append({"bbox": list(bbox) if bbox else None, "head_width": head_width, "cheek_width": cheek_width, "feet_anchor_y": FEET_Y})
        atlas = Image.new("RGBA", (CELL[0] * 3, CELL[1]))
        for index, frame in enumerate(frames):
            atlas.alpha_composite(frame, (CELL[0] * index, 0))
        atlas.save(OUT_DIR / f"mnema-{state}.png")
        rendered[state] = tuple(frames)
        report["states"][state] = {"frames": metrics, "source_scale": STRIP_SCALE}

    with Image.open(RAW_DIR / "alert.png") as raw:
        alert_source = raw.convert("RGBA")
    if alert_source.size != (1403, 1121):
        raise ValueError(f"alert.png must be 1403x1121, got {alert_source.size}")
    # Alert is a portrait source, calibrated only against Mnema's idle head width.
    alert = _place_scaled(alert_source, ALERT_SCALE, 1107)
    alert.save(OUT_DIR / "mnema-alert.png")
    rendered["alert"] = (alert,)
    alert_bbox = _bbox(alert)
    report["states"]["alert"] = {"frames": [{"bbox": list(alert_bbox) if alert_bbox else None, "head_width": _head_and_cheek_width(alert)[0], "cheek_width": _head_and_cheek_width(alert)[1], "feet_anchor_y": FEET_Y}], "source_scale": ALERT_SCALE}

    _steam_sheet().save(OUT_DIR / "mnema-steam.png")
    atlas = {"cell": list(CELL), "feetAnchor": [CENTER_X, FEET_Y], "sheets": {**{f"mnema-{state}.png": [0, 1, 2] for state in STATES}, "mnema-alert.png": [0], "mnema-steam.png": list(range(24))}}
    (OUT_DIR / "atlas.json").write_text(json.dumps(atlas, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT_DIR / "asset-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _contact_sheet(rendered)
    return report


if __name__ == "__main__":
    build()
