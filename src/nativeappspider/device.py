"""ADB device interface for screenshots, input, and UI hierarchy."""

from __future__ import annotations

import io
import logging
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from PIL import Image

logger = logging.getLogger(__name__)


class ADBError(Exception):
    """Raised when an ADB command fails."""


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
    """Parse '[x1,y1][x2,y2]' into (x1, y1, x2, y2).

    This is the bounds format used by Android's uiautomator XML dump.
    """
    try:
        # "[x1,y1][x2,y2]" → "x1,y1,x2,y2" → split into four ints
        parts = bounds_str.replace("][", ",").strip("[]").split(",")
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    except (ValueError, IndexError):
        return (0, 0, 0, 0)


class Device:
    """Controls an Android device/emulator via ADB."""

    def __init__(self, serial: str | None = None):
        self._serial = serial
        # When a serial is provided, all ADB commands target that specific device.
        # Without it, ADB uses whatever single device/emulator is connected.
        self._adb_prefix = ["adb"]
        if serial:
            self._adb_prefix = ["adb", "-s", serial]
        self._screen_size: tuple[int, int] | None = None  # cached after first query

    def _exec(
        self, *args: str, timeout: int = 30, binary: bool = False, strict: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run an ADB command. Core method for all ADB interactions."""
        cmd = [*self._adb_prefix, *args]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=not binary, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ADBError(f"ADB command timed out after {timeout}s: {' '.join(cmd)}") from e
        except FileNotFoundError as e:
            raise ADBError("ADB not found on PATH. Install Android SDK Platform Tools.") from e

        if strict and result.returncode != 0:
            stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
            raise ADBError(
                f"ADB command failed (exit {result.returncode}): {' '.join(cmd)}\n{stderr.strip()}"
            )
        return result

    def _run(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return self._exec(*args, timeout=timeout)

    def _run_binary(self, *args: str, timeout: int = 30) -> bytes:
        return self._exec(*args, timeout=timeout, binary=True).stdout

    def is_connected(self) -> bool:
        """Check if a device is connected and accessible."""
        try:
            result = self._run("devices")
        except ADBError:
            return False

        lines = result.stdout.strip().splitlines()
        # `adb devices` output: header line, then "<serial>\t<state>" per device.
        # A device is usable only when its state is "device" (not "offline"/"unauthorized").
        for line in lines[1:]:  # Skip header
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                if self._serial is None or parts[0] == self._serial:
                    return True
        return False

    def get_screen_size(self) -> tuple[int, int]:
        """Get the device screen size in pixels (width, height). Cached after first call.

        Prefers the 'Override size' if set (e.g. via `adb shell wm size 1080x1920`),
        falls back to 'Physical size'.
        """
        if self._screen_size is not None:
            return self._screen_size

        result = self._run("shell", "wm", "size")
        # Output may contain:
        #   Physical size: 320x640
        #   Override size: 1080x1920
        # Prefer override when present.
        physical: tuple[int, int] | None = None
        for line in result.stdout.strip().splitlines():
            if ":" not in line or "x" not in line:
                continue
            label, size_str = line.split(":", 1)
            size_str = size_str.strip()
            try:
                w, h = size_str.split("x")
                parsed = (int(w), int(h))
            except (ValueError, IndexError):
                continue

            if "override" in label.lower():
                self._screen_size = parsed
                return self._screen_size
            if physical is None:
                physical = parsed

        if physical:
            self._screen_size = physical
            return self._screen_size

        raise ADBError(f"Could not parse screen size from: {result.stdout.strip()}")

    def screenshot(self) -> Image.Image:
        """Capture a screenshot and return as PIL Image."""
        # exec-out streams raw bytes directly (no temp file on device needed).
        # The -p flag outputs PNG format.
        data = self._run_binary("exec-out", "screencap", "-p", timeout=10)

        # A valid PNG is always >100 bytes; anything smaller means the capture failed
        if len(data) < 100:
            raise ADBError(f"Screenshot too small ({len(data)} bytes) — likely corrupt or empty")

        try:
            return Image.open(io.BytesIO(data))
        except Exception as e:
            raise ADBError(f"Failed to decode screenshot ({len(data)} bytes): {e}") from e

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
        """Type text (spaces must be escaped for ADB).

        ADB's `input text` interprets %s as a space — literal spaces would be
        treated as argument separators by the shell.
        """
        escaped = text.replace(" ", "%s")
        self._run("shell", "input", "text", escaped)

    def current_activity(self) -> str:
        """Get the current foreground activity name."""
        try:
            result = self._run("shell", "dumpsys", "activity", "activities")
        except ADBError:
            return "unknown"

        # Different Android versions use different field names for the
        # foreground activity — check all known variants for compatibility
        indicators = ["topResumedActivity", "mResumedActivity", "mFocusedActivity", "mFocusedApp"]
        for line in result.stdout.splitlines():
            if any(ind in line for ind in indicators):
                parts = line.strip().split()
                # Activity names look like "com.example.app/.MainActivity" — they
                # contain both "/" and "." which distinguishes them from other tokens
                for part in parts:
                    if "/" in part and "." in part:
                        return part.rstrip("}")
        return "unknown"

    def clear_app_data(self, package: str) -> None:
        """Clear all app data (like a fresh install). App must be installed."""
        self._run("shell", "pm", "clear", package)

    def is_package_installed(self, package: str) -> bool:
        """Check if a package is installed on the device."""
        result = self._run("shell", "pm", "list", "packages", package)
        return f"package:{package}" in result.stdout

    def launch_app(self, package: str) -> None:
        """Launch an app by package name.

        Uses `am start` with the launcher intent. Falls back to `monkey` if
        the main activity can't be resolved (e.g. some apps don't export their
        main activity in a way resolve-activity can find).
        """
        try:
            self._run(
                "shell", "am", "start",
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER",
                "-n", self._resolve_main_activity(package),
            )
        except ADBError:
            # Monkey sends a launcher intent without needing the activity name,
            # but its exit code is unreliable so we don't check it
            self._run_lenient(
                "shell", "monkey", "-p", package,
                "-c", "android.intent.category.LAUNCHER", "1",
            )

    def _resolve_main_activity(self, package: str) -> str:
        """Find the main launcher activity for a package."""
        result = self._run("shell", "cmd", "package", "resolve-activity",
                           "--brief", "-a", "android.intent.action.MAIN",
                           "-c", "android.intent.category.LAUNCHER", package)
        lines = result.stdout.strip().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if "/" in line and "." in line:
                return line
        raise ADBError(f"Could not resolve main activity for {package}")

    def _run_lenient(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return self._exec(*args, timeout=timeout, strict=False)

    def get_ui_hierarchy(self) -> list[UIElement]:
        """Dump and parse the UI hierarchy.

        Uses uiautomator to dump the current view tree to an XML file on
        the device, then reads it back. This gives us structured info about
        every visible element (class, bounds, clickability, text, etc.).
        """
        try:
            self._run("shell", "uiautomator", "dump", "/sdcard/ui_dump.xml")
            result = self._run("shell", "cat", "/sdcard/ui_dump.xml")
        except ADBError as e:
            logger.warning("UI hierarchy dump failed: %s", e)
            return []

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
