"""Refresh README GIF timing and compose verified native bubble QA surfaces.

This script never redraws product UI. It only re-times approved GIF frames and
composites captured native WebView surfaces with already-approved monitor and
character captures.
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
    durations = [180] * len(frames)
    durations[-1] = 500
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


def resize_to_width(image: Image.Image, width: int) -> Image.Image:
    height = round(image.height * width / image.width)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def compose_bubbles(
    character_path: Path,
    monitor_path: Path,
    input_path: Path,
    speech_path: Path,
    output: Path,
) -> None:
    background = (39, 39, 39, 255)
    canvas = Image.new("RGBA", (1280, 720), background)
    with Image.open(character_path) as raw:
        character = resize_to_width(edge_alpha(raw), 220)
    with Image.open(monitor_path) as raw:
        monitor_surface = raw.convert("RGBA").crop((0, 0, raw.width, 220))
        monitor = resize_to_width(monitor_surface, 250)
    with Image.open(input_path) as raw:
        input_bubble = resize_to_width(raw.convert("RGBA"), 640)
    with Image.open(speech_path) as raw:
        speech = resize_to_width(raw.convert("RGBA"), 640)
    # Dense 16:9 documentation layout: monitor/character on the left and the
    # active response plus composer on the right. Every foreground surface is
    # a captured product surface; this function only changes scale/placement.
    canvas.alpha_composite(monitor, (42, 44))
    canvas.alpha_composite(character, (77, 430))
    canvas.alpha_composite(speech, (565, 72))
    canvas.alpha_composite(input_bubble, (565, 470))
    canvas.convert("RGB").save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--input-capture", type=Path, required=True)
    parser.add_argument("--speech-capture", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--shell-exe", type=Path, required=True)
    args = parser.parse_args()

    asset_dir = args.asset_dir.resolve()
    gif = asset_dir / "native-bolttagu-trickcal-v1.5.15.gif"
    poster = asset_dir / "native-bolttagu-trickcal-v1.5.15.png"
    bubble = asset_dir / "bubble-mode-history-20260912.png"
    timing = retime_gif(gif, gif)
    monitor = asset_dir / "session-monitor-trickcal-v1.5.15.png"
    compose_bubbles(
        poster,
        monitor,
        args.input_capture.resolve(),
        args.speech_capture.resolve(),
        bubble,
    )

    provenance_path = asset_dir / "trickcal-demo-v1.5.15.provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["evidence"]["gif_encoded_frames"] = timing["frames"]
    provenance["evidence"]["gif_duration_ms"] = timing["duration_ms"]
    provenance["evidence"]["gif_frame_durations_ms"] = timing["durations_ms"]
    provenance["evidence"]["gif_timing_refresh"] = "Same approved captured frames re-encoded at a regular 180 ms cadence with a 500 ms final hold; no generated or interpolated art."
    provenance["evidence"]["bubble"] = "Actual source-native synthetic-QA input and active response captures; no provider query or private reply."
    provenance["evidence"]["bubble_composite"] = {
        "runtime": "source native Tauri shell with synthetic QA host",
        "source_commit": args.source_commit,
        "shell_exe_sha256": sha256(args.shell_exe.resolve()),
        "input_capture_sha256": sha256(args.input_capture.resolve()),
        "speech_capture_sha256": sha256(args.speech_capture.resolve()),
        "monitor_capture_sha256": sha256(monitor),
        "character_capture_sha256": sha256(poster),
        "composition": "Actual source-runtime RGBA input and active-speech WebView captures, plus the upper session-stack crop and character from approved installed captures, on a neutral matte. Crop, scale and placement only; no UI redraw.",
    }
    provenance["evidence"].pop("bubble_history", None)
    provenance["sha256"][gif.name] = sha256(gif)
    provenance["sha256"][bubble.name] = sha256(bubble)
    limitation = "The dense bubble README composition is source-runtime synthetic QA, not a provider conversation or frozen-release proof."
    legacy_limitation = "The response-history README composition is source-runtime synthetic QA, not a provider conversation or frozen-release proof."
    provenance["limitations"] = [
        item for item in provenance["limitations"]
        if item not in {limitation, legacy_limitation}
    ]
    provenance["limitations"].append(limitation)
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
