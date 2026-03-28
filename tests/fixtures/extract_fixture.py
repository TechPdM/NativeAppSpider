#!/usr/bin/env python3
"""Extract a compact test fixture from a crawl recording.

Usage:
    python tests/fixtures/extract_fixture.py <crawl-dir> <fixture-dir>

Reads recording.json from the crawl output, downscales screenshots,
and writes a self-contained fixture directory with scenario.json and
compact screenshots suitable for checking into the repo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

# Downscale screenshots to this size to keep fixtures small
FIXTURE_WIDTH = 270
FIXTURE_HEIGHT = 480


def extract(crawl_dir: Path, fixture_dir: Path) -> None:
    recording_path = crawl_dir / "recording.json"
    if not recording_path.exists():
        print(f"Error: {recording_path} not found. Run crawl with --record first.")
        sys.exit(1)

    recording = json.loads(recording_path.read_text())

    fixture_dir.mkdir(parents=True, exist_ok=True)
    ss_dir = fixture_dir / "screenshots"
    ss_dir.mkdir(exist_ok=True)

    # Process each step: downscale screenshots and rewrite paths
    for i, step in enumerate(recording["steps"]):
        # Read and downscale the original screenshot
        original_path = crawl_dir / step["screenshot_path"]
        fixture_ss_name = f"{i:03d}.png"

        if original_path.exists():
            img = Image.open(original_path)
            img = img.resize((FIXTURE_WIDTH, FIXTURE_HEIGHT), Image.LANCZOS)
            img.save(ss_dir / fixture_ss_name)
        else:
            # Create a placeholder if the original is missing
            img = Image.new("RGB", (FIXTURE_WIDTH, FIXTURE_HEIGHT), (128, 128, 128))
            img.save(ss_dir / fixture_ss_name)
            print(f"  Warning: {original_path} not found, using placeholder")

        # Rewrite the path to be relative to fixture dir
        step["screenshot_path"] = f"screenshots/{fixture_ss_name}"

    # Write the scenario file
    scenario = {
        "config": recording["config"],
        "steps": recording["steps"],
    }
    (fixture_dir / "scenario.json").write_text(json.dumps(scenario, indent=2))

    total_size = sum(f.stat().st_size for f in ss_dir.glob("*.png"))
    print(f"Extracted {len(recording['steps'])} steps to {fixture_dir}")
    print(f"Screenshots: {len(list(ss_dir.glob('*.png')))} files, {total_size / 1024:.0f} KB total")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <crawl-dir> <fixture-dir>")
        sys.exit(1)
    extract(Path(sys.argv[1]), Path(sys.argv[2]))
