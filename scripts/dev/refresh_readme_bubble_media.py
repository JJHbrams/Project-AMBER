"""Refresh README GIF timing and compose verified native bubble QA surfaces.

This script never redraws product UI. It only re-times approved GIF frames and
composites captured native WebView surfaces with already-approved monitor and
character captures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import io
import subprocess
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
    # Immutable original capture, so repeated runs never consume edited output.
    original = subprocess.run(
        ['git', 'show', 'b9377b0:resource/asset/readme/native-bolttagu-trickcal-v1.5.15.gif'],
        cwd=Path(__file__).resolve().parents[2], check=True, capture_output=True,
    ).stdout
    with Image.open(io.BytesIO(original)) as raw:
        frames = []
        for index in range(raw.n_frames):
            raw.seek(index)
            frames.append(raw.convert("RGBA").copy())
    captured = frames
    frames, durations = [], []
    # Each scene keeps its own prop and facing direction. Fade through the
    # matte between scenes instead of implying a physically continuous action.
    scenes = [([0, 1, 2, 3], [2400, 100, 100, 1200]),
              ([12], [3400]),
              ([19, 20, 21, 22, 23], [1800, 220, 220, 220, 1400]),
              ([24, 25, 26], [2000, 200, 1400]),
              ([13], [3200])]
    matte = Image.new('RGBA', captured[0].size, (0, 0, 0, 255))
    for indexes, timing in scenes:
        first, last = captured[indexes[0]], captured[indexes[-1]]
        for alpha in (.2, .4, .6, .8):
            frames.append(Image.blend(matte, first, alpha)); durations.append(100)
        for index, duration in zip(indexes, timing):
            frames.append(captured[index]); durations.append(duration)
        for alpha in (.8, .6, .4, .2):
            frames.append(Image.blend(matte, last, alpha)); durations.append(100)
        frames.append(matte.copy()); durations.append(160)
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
    canvas = Image.new("RGBA", (800, 500), background)
    with Image.open(character_path) as raw:
        character = edge_alpha(raw)
        character = character.crop(character.getbbox())
        character = character.resize((round(character.width * 210 / character.height), 210), Image.Resampling.LANCZOS)
    with Image.open(monitor_path) as raw:
        monitor_surface = raw.convert("RGBA").crop((0, 0, raw.width, 220))
        monitor = resize_to_width(monitor_surface, 300)
    with Image.open(input_path) as raw:
        # Exclude the separate QA preview launcher below the native window.
        input_bubble = raw.convert("RGBA").crop((0, 0, raw.width, 164))
    with Image.open(speech_path) as raw:
        speech = raw.convert("RGBA")
    # Shared desktop-scale layout: monitor/character on the left and the
    # active response plus composer on the right. Every foreground surface is
    # a captured product surface; this function only changes scale/placement.
    canvas.alpha_composite(monitor, (20, 65))
    canvas.alpha_composite(character, (95, 270))
    canvas.alpha_composite(speech, (350, 95))
    canvas.alpha_composite(input_bubble, (300, 325))
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
    provenance["evidence"]["events"] = "Five separate scenes: coffee idle, reading, writing, success, alert; consistent facing direction."
    provenance["evidence"]["gif_duration_ms"] = timing["duration_ms"]
    provenance["evidence"]["gif_frame_durations_ms"] = timing["durations_ms"]
    provenance["evidence"]["gif_timing_refresh"] = "Original b9377b0 capture grouped into five scenes held for 3.2-3.86 seconds each, with 400ms fade-out, 160ms matte pause, and 400ms fade-in between props. Blink remains 100ms. No invented in-between character poses."
    provenance["evidence"]["bubble"] = "Actual source-native synthetic-QA input and active response captures; no provider query or private reply."
    provenance["evidence"]["bubble_composite"] = {
        "runtime": "source native Tauri shell with synthetic QA host",
        "source_commit": args.source_commit,
        "shell_exe_sha256": sha256(args.shell_exe.resolve()),
        "input_capture_sha256": sha256(args.input_capture.resolve()),
        "speech_capture_sha256": sha256(args.speech_capture.resolve()),
        "monitor_capture_sha256": sha256(monitor),
        "character_capture_sha256": sha256(poster),
        "composition": "800x500 desktop-scale composition: character silhouette 210px high, monitor 300px wide above it, speech and input at original capture scale. Separate QA preview launcher cropped below input. No UI redraw.",
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
