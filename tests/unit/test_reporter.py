"""Tests for HTML report generation."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from nativeappspider.reporter import generate_html_report


def _create_crawl_fixtures(tmp_path: Path) -> Path:
    """Create minimal crawl output files for testing."""
    crawl_dir = tmp_path / "crawl_output"
    crawl_dir.mkdir()
    (crawl_dir / "screenshots").mkdir()

    # Save a test screenshot
    img = Image.new("RGB", (100, 200), (128, 128, 128))
    ss_path = crawl_dir / "screenshots" / "abc123.png"
    img.save(ss_path)

    screens = {
        "hash1": {
            "screen_name": "Home Screen",
            "description": "Main landing page",
            "activity": "com.app/.MainActivity",
            "elements": [
                {"label": "Settings", "type": "button", "purpose": "Open settings"}
            ],
            "screenshot": str(ss_path),
            "visit_count": 3,
            "first_seen": "2024-01-01T00:00:00",
        },
        "hash2": {
            "screen_name": "Settings",
            "description": "App settings page",
            "activity": "com.app/.SettingsActivity",
            "elements": [],
            "screenshot": str(crawl_dir / "screenshots" / "missing.png"),  # Missing file
            "visit_count": 1,
            "first_seen": "2024-01-01T00:01:00",
        },
    }

    transitions = [
        {"from": "Home Screen", "to": "Settings", "action": "tap", "reason": "explore settings"},
    ]

    mermaid = 'graph TD\n    S0["Home Screen"]\n    S1["Settings"]\n    S0 -->|tap| S1\n'

    (crawl_dir / "screens.json").write_text(json.dumps(screens))
    (crawl_dir / "transitions.json").write_text(json.dumps(transitions))
    (crawl_dir / "flow.mmd").write_text(mermaid)

    return crawl_dir


def test_generates_html(tmp_path):
    crawl_dir = _create_crawl_fixtures(tmp_path)
    report = generate_html_report(crawl_dir)
    assert report.exists()
    assert report.name == "report.html"


def test_html_contains_screen_names(tmp_path):
    crawl_dir = _create_crawl_fixtures(tmp_path)
    report = generate_html_report(crawl_dir)
    html = report.read_text()
    assert "Home Screen" in html
    assert "Settings" in html


def test_html_contains_mermaid(tmp_path):
    crawl_dir = _create_crawl_fixtures(tmp_path)
    report = generate_html_report(crawl_dir)
    html = report.read_text()
    assert "mermaid" in html
    assert "graph TD" in html


def test_html_embeds_screenshot_base64(tmp_path):
    crawl_dir = _create_crawl_fixtures(tmp_path)
    report = generate_html_report(crawl_dir)
    html = report.read_text()
    assert "data:image/png;base64," in html


def test_html_handles_missing_screenshot(tmp_path):
    crawl_dir = _create_crawl_fixtures(tmp_path)
    report = generate_html_report(crawl_dir)
    html = report.read_text()
    assert "No screenshot" in html


def test_html_contains_stats(tmp_path):
    crawl_dir = _create_crawl_fixtures(tmp_path)
    report = generate_html_report(crawl_dir)
    html = report.read_text()
    assert ">2<" in html  # 2 screens
    assert ">1<" in html  # 1 transition
