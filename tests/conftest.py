"""Shared test fixtures."""

from __future__ import annotations

import pytest
from PIL import Image

SAMPLE_UI_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,1920]" scrollable="false">
    <node index="0" text="Settings" resource-id="com.app:id/title"
          class="android.widget.TextView" content-desc=""
          clickable="true" enabled="true" bounds="[0,0][540,96]" scrollable="false" />
    <node index="1" text="" resource-id="com.app:id/search_btn"
          class="android.widget.ImageButton" content-desc="Search"
          clickable="true" enabled="true" bounds="[900,0][1080,96]" scrollable="false" />
    <node index="2" text="Network" resource-id="com.app:id/network"
          class="android.widget.LinearLayout" content-desc=""
          clickable="true" enabled="true" bounds="[0,200][1080,350]" scrollable="false" />
    <node index="3" text="Display" resource-id="com.app:id/display"
          class="android.widget.LinearLayout" content-desc=""
          clickable="true" enabled="false" bounds="[0,350][1080,500]" scrollable="false" />
    <node index="4" text="" resource-id=""
          class="android.widget.ScrollView" content-desc=""
          clickable="false" enabled="true" bounds="[0,96][1080,1920]" scrollable="true" />
  </node>
</hierarchy>"""


@pytest.fixture
def sample_image() -> Image.Image:
    """A simple 100x200 test image."""
    return Image.new("RGB", (100, 200), color=(128, 128, 128))


@pytest.fixture
def sample_image_similar() -> Image.Image:
    """An image very similar to sample_image (slight color change)."""
    return Image.new("RGB", (100, 200), color=(130, 128, 128))


@pytest.fixture
def sample_ui_xml() -> str:
    return SAMPLE_UI_XML
