"""ADB device interface for screenshots, input, and UI hierarchy."""

from __future__ import annotations

import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass
class UIElement:
    """A single UI element from the view hierarchy."""

    resource_id: str
    class_name: str
    text: str
    content_desc: str
    bounds: tuple[int, int, int, int]  # x1, y1, x2, y2
    clickable: bool
    scrollable: bool
    enabled: bool

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def label(self) -> str:
        """Best human-readable label for this element."""
        return self.text or self.content_desc or self.resource_id or self.class_name


def _parse_bounds(bounds_str: str) -> tuple[int, int, int, int]:
    """Parse '[x1,y1][x2,y2]' into (x1, y1, x2, y2)."""
    parts = bounds_str.replace("][", ",").strip("[]").split(",")
    return tuple(int(p) for p in parts)  # type: ignore[return-value]


class Device:
    """Controls an Android device/emulator via ADB."""

    def __init__(self, serial: str | None = None):
        self._serial = serial
        self._adb_prefix = ["adb"]
        if serial:
            self._adb_prefix = ["adb", "-s", serial]

    def _run(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        cmd = [*self._adb_prefix, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def screenshot(self) -> Image.Image:
        """Capture a screenshot and return as PIL Image."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = f.name
        self._run("exec-out", "screencap", "-p", timeout=10)
        # Use exec-out for binary data
        result = subprocess.run(
            [*self._adb_prefix, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=10,
        )
        Path(tmp).write_bytes(result.stdout)
        return Image.open(tmp)

    def tap(self, x: int, y: int) -> None:
        """Tap at coordinates."""
        self._run("shell", "input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        """Swipe between coordinates."""
        self._run("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def press_back(self) -> None:
        """Press the back button."""
        self._run("shell", "input", "keyevent", "4")

    def press_home(self) -> None:
        """Press the home button."""
        self._run("shell", "input", "keyevent", "3")

    def input_text(self, text: str) -> None:
        """Type text (spaces must be escaped for ADB)."""
        escaped = text.replace(" ", "%s")
        self._run("shell", "input", "text", escaped)

    def current_activity(self) -> str:
        """Get the current foreground activity name."""
        result = self._run("shell", "dumpsys", "activity", "activities")
        for line in result.stdout.splitlines():
            if "mResumedActivity" in line or "mFocusedActivity" in line:
                # Extract activity name from the line
                parts = line.strip().split()
                for part in parts:
                    if "/" in part and "." in part:
                        return part.rstrip("}")
        return "unknown"

    def launch_app(self, package: str) -> None:
        """Launch an app by package name using monkey."""
        self._run(
            "shell", "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1",
        )

    def get_ui_hierarchy(self) -> list[UIElement]:
        """Dump and parse the UI hierarchy."""
        self._run("shell", "uiautomator", "dump", "/sdcard/ui_dump.xml")
        result = self._run("shell", "cat", "/sdcard/ui_dump.xml")
        if not result.stdout.strip():
            return []
        return self._parse_hierarchy(result.stdout)

    @staticmethod
    def _parse_hierarchy(xml_str: str) -> list[UIElement]:
        """Parse uiautomator XML into UIElement list."""
        elements: list[UIElement] = []
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            return elements

        for node in root.iter("node"):
            bounds_str = node.get("bounds", "[0,0][0,0]")
            elements.append(UIElement(
                resource_id=node.get("resource-id", ""),
                class_name=node.get("class", ""),
                text=node.get("text", ""),
                content_desc=node.get("content-desc", ""),
                bounds=_parse_bounds(bounds_str),
                clickable=node.get("clickable") == "true",
                scrollable=node.get("scrollable") == "true",
                enabled=node.get("enabled") == "true",
            ))
        return elements

    def get_clickable_elements(self) -> list[UIElement]:
        """Get only clickable, enabled elements."""
        return [e for e in self.get_ui_hierarchy() if e.clickable and e.enabled]
