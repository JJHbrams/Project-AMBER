#!/usr/bin/env python3
"""Render the shared LLM creed into provider policy files."""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "llm" / "creed.md"
TARGETS = (ROOT / "AGENTS.md", ROOT / "CLAUDE.md")
BANNER = "<!-- GENERATED from docs/llm/creed.md; do not edit directly. -->\n\n"


def expected() -> str:
    return BANNER + SOURCE.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n") + "\n"


def render(check: bool) -> int:
    content = expected()
    drifted = [target for target in TARGETS if not target.is_file() or target.read_text(encoding="utf-8").replace("\r\n", "\n") != content]
    if check:
        if drifted:
            for target in drifted:
                print(f"drift: {target.relative_to(ROOT)}")
            return 1
        print("LLM creed generated files are current.")
        return 0
    for target in TARGETS:
        target.write_text(content, encoding="utf-8", newline="\n")
        print(f"rendered: {target.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the shared LLM creed.")
    parser.add_argument("--check", action="store_true", help="fail when generated files drift")
    return render(parser.parse_args().check)


if __name__ == "__main__":
    raise SystemExit(main())
