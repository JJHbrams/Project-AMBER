"""Refresh README GIF timing and compose verified native bubble QA surfaces.

This script never redraws product UI.  It only re-times the approved GIF frames
and composites captured RGBA WebView surfaces with the approved character poster.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def edge_alpha(image: Image.Image) -> Image.Image:
    image = image.convert("RGBA")
    pixels = image.load()
    width, height = image.size
    pending = [(x, 0) for x in range(width)] + [(x, height - 1) for x in range(width)]
    pending += [(0, y) for y in range(height)] + [(width - 1, y) for y in range(height)]
    exterior: set[tuple[int, int]] = set()
    while pending:
        x, y = pending.pop()
        if (x, y) in exterior or not (0 <= x < width and 0 <= y < height):
            continue
        red, green, blue, _ = pixels[x, y]
        if max(red, green, blue) > 8:
            continue
        exterior.add((x, y))
        pending.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    for x, y in exterior:
        pixels[x, y] = (0, 0, 0, 0)
    return image


def retime_gif(source: Path, output: Path) -> dict:
    with Image.open(source) as raw:
        frames = []
        for index in range(raw.n_frames):
            raw.seek(index)
            frames.append(raw.convert("RGBA").copy())
    durations = [120] * len(frames)
    durations[-1] = 240
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )
    with Image.open(output) as encoded:
        actual = []
        for index in range(encoded.n_frames):
            encoded.seek(index)
            actual.append(encoded.info.get("duration", 0))
    return {"frames": len(actual), "duration_ms": sum(actual), "durations_ms": actual}


def compose_bubbles(character_path: Path, input_path: Path, history_path: Path, output: Path) -> None:
    background = (39, 39, 39, 255)
    canvas = Image.new("RGBA", (980, 650), background)
    with Image.open(character_path) as raw:
        character = edge_alpha(raw)
    with Image.open(input_path) as raw:
        input_bubble = raw.convert("RGBA")
    with Image.open(history_path) as raw:
        history = raw.convert("RGBA")
    canvas.alpha_composite(character, (28, 326))
    canvas.alpha_composite(history, (265, 22))
    canvas.alpha_composite(input_bubble, (425, 330))
    canvas.convert("RGB").save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--input-capture", type=Path, required=True)
    parser.add_argument("--history-capture", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()

    asset_dir = args.asset_dir.resolve()
    gif = asset_dir / "native-bolttagu-trickcal-v1.5.15.gif"
    poster = asset_dir / "native-bolttagu-trickcal-v1.5.15.png"
    bubble = asset_dir / "bubble-mode-history-20260912.png"
    timing = retime_gif(gif, gif)
    compose_bubbles(poster, args.input_capture.resolve(), args.history_capture.resolve(), bubble)

    provenance_path = asset_dir / "trickcal-demo-v1.5.15.provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["evidence"]["gif_encoded_frames"] = timing["frames"]
    provenance["evidence"]["gif_duration_ms"] = timing["duration_ms"]
    provenance["evidence"]["gif_frame_durations_ms"] = timing["durations_ms"]
    provenance["evidence"]["gif_timing_refresh"] = "Same approved captured frames re-encoded only; no generated or interpolated art."
    provenance["evidence"]["bubble_history"] = {
        "runtime": "source native Tauri shell with synthetic QA host",
        "source_commit": args.source_commit,
        "input_capture_sha256": sha256(args.input_capture.resolve()),
        "history_capture_sha256": sha256(args.history_capture.resolve()),
        "composition": "Actual RGBA WebView captures plus the approved installed character poster on a neutral matte.",
    }
    provenance["sha256"][gif.name] = sha256(gif)
    provenance["sha256"][bubble.name] = sha256(bubble)
    limitation = "The response-history README composition is source-runtime synthetic QA, not a provider conversation or frozen-release proof."
    provenance["limitations"] = [item for item in provenance["limitations"] if item != limitation]
    provenance["limitations"].append(limitation)
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
