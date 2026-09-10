"""Build the public native-Bolttagu README animation from verified captures only.

This deliberately accepts a capture directory rather than rendering art.  It
keeps the public asset reproducible and makes a mock, local overlay, or a
different binary impossible to substitute silently.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image


EXPECTED_EXE_SHA256 = "590f44f1ac28b016eb1b2920a2cfdd69e0195e33fbe86dd18bc7d62162280818"
FRAME_NAMES = (
    "native-idle.png", "state-1.png", "state-2.png", "state-3.png",
    "completed-idle.png", "idle-sequence-0.png", "idle-sequence-1.png",
    "idle-sequence-2.png", "idle-sequence-3.png", "idle-sequence-4.png",
    "idle-sequence-5.png", "idle-sequence-6.png", "idle-sequence-7.png",
)
DURATIONS_MS = (800, 850, 850, 850, 800, 90, 90, 90, 90, 90, 90, 90, 90)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source: Path, output_dir: Path) -> None:
    report = json.loads((source / "report.json").read_text(encoding="utf-8"))
    if report.get("exe_sha256") != EXPECTED_EXE_SHA256:
        raise ValueError("capture report is not the approved v1.5.15.736 executable")
    if report.get("result") != "PASS: bounded frozen smoke only":
        raise ValueError("capture report is not a passing bounded frozen smoke")
    capture_hashes = {item["name"]: item["pixel_sha256"] for item in report.get("captures", [])}
    images: list[Image.Image] = []
    provenance_frames: list[dict[str, str]] = []
    for name in FRAME_NAMES:
        path = source / name
        if not path.is_file():
            raise FileNotFoundError(path)
        if name.removesuffix(".png") not in capture_hashes:
            raise ValueError(f"{name} is not an owned-HWND capture in report.json")
        with Image.open(path) as raw:
            image = raw.convert("RGBA")
        images.append(image)
        provenance_frames.append({
            "name": name,
            "file_sha256": _sha256(path),
            "reported_pixel_sha256": capture_hashes[name.removesuffix(".png")],
        })
    if len({image.size for image in images}) != 1:
        raise ValueError("capture dimensions differ")
    output_dir.mkdir(parents=True, exist_ok=True)
    poster = output_dir / "native-bolttagu-v1.5.15.png"
    animation = output_dir / "native-bolttagu-v1.5.15.gif"
    images[0].save(poster, format="PNG")
    images[0].save(
        animation, format="GIF", save_all=True, append_images=images[1:],
        duration=DURATIONS_MS, loop=0, disposal=2,
    )
    provenance = {
        "schema_version": 1,
        "asset": animation.name,
        "poster": poster.name,
        "frame_count": len(FRAME_NAMES),
        "source_runtime": "frozen-normal-launch",
        "source_version": "1.5.15.736",
        "exe_sha256": EXPECTED_EXE_SHA256,
        "source_result": "PASS: bounded frozen smoke only",
        "frames": provenance_frames,
        "limitations": [
            "Synthetic metadata, not provider lifecycle evidence.",
            "Owned-window launcher click; original pointer restored.",
            "Normal frozen launch is not an Inno installation test.",
        ],
    }
    (output_dir / "native-bolttagu-v1.5.15.provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="verified capture directory")
    parser.add_argument("--output", type=Path, default=Path("resource/asset/readme"))
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
