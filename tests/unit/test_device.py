"""Tests for ADB device interface."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from appspider.device import ADBError, Device, UIElement, _parse_bounds


# --- _parse_bounds ---

def test_parse_bounds_valid():
    assert _parse_bounds("[0,0][1080,1920]") == (0, 0, 1080, 1920)


def test_parse_bounds_nonzero():
    assert _parse_bounds("[100,200][300,400]") == (100, 200, 300, 400)


def test_parse_bounds_malformed():
    assert _parse_bounds("garbage") == (0, 0, 0, 0)


def test_parse_bounds_empty():
    assert _parse_bounds("") == (0, 0, 0, 0)


# --- UIElement ---

def test_ui_element_center():
    el = UIElement("id", "cls", "text", "desc", (0, 0, 100, 200), True, False, True)
    assert el.center == (50, 100)


def test_ui_element_label_prefers_text():
    el = UIElement("id", "cls", "Submit", "desc", (0, 0, 0, 0), True, False, True)
    assert el.label == "Submit"


def test_ui_element_label_falls_back_to_content_desc():
    el = UIElement("id", "cls", "", "Search", (0, 0, 0, 0), True, False, True)
    assert el.label == "Search"


def test_ui_element_label_falls_back_to_resource_id():
    el = UIElement("com.app:id/btn", "cls", "", "", (0, 0, 0, 0), True, False, True)
    assert el.label == "com.app:id/btn"


def test_ui_element_label_falls_back_to_class():
    el = UIElement("", "android.widget.Button", "", "", (0, 0, 0, 0), True, False, True)
    assert el.label == "android.widget.Button"


# --- Device._run error handling ---

def test_run_raises_on_nonzero_exit():
    device = Device()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["adb", "shell", "echo"],
            returncode=1,
            stdout="",
            stderr="error: device not found",
        )
        with pytest.raises(ADBError, match="device not found"):
            device._run("shell", "echo")


def test_run_raises_on_timeout():
    device = Device()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=30)):
        with pytest.raises(ADBError, match="timed out"):
            device._run("shell", "sleep", "100")


def test_run_raises_on_adb_not_found():
    device = Device()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(ADBError, match="ADB not found"):
            device._run("devices")


def test_run_success():
    device = Device()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["adb", "devices"], returncode=0, stdout="ok", stderr="",
        )
        result = device._run("devices")
        assert result.stdout == "ok"


# --- Device.is_connected ---

def test_is_connected_true():
    device = Device()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="List of devices attached\nemulator-5554\tdevice\n",
            stderr="",
        )
        assert device.is_connected() is True


def test_is_connected_no_devices():
    device = Device()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="List of devices attached\n\n",
            stderr="",
        )
        assert device.is_connected() is False


def test_is_connected_specific_serial():
    device = Device(serial="emulator-5556")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n",
            stderr="",
        )
        assert device.is_connected() is True


def test_is_connected_wrong_serial():
    device = Device(serial="emulator-5558")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="List of devices attached\nemulator-5554\tdevice\n",
            stderr="",
        )
        assert device.is_connected() is False


def test_is_connected_adb_fails():
    device = Device()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert device.is_connected() is False


# --- Device.get_screen_size ---

def test_get_screen_size():
    device = Device()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Physical size: 1080x1920\n", stderr="",
        )
        assert device.get_screen_size() == (1080, 1920)


def test_get_screen_size_cached():
    device = Device()
    device._screen_size = (720, 1280)
    assert device.get_screen_size() == (720, 1280)


# --- Device.screenshot ---

def test_screenshot_returns_image():
    device = Device()
    # Create a valid PNG large enough to pass the size check
    img = Image.new("RGB", (100, 100), (255, 0, 0))
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=png_bytes, stderr=b"")
        result = device.screenshot()
        assert isinstance(result, Image.Image)


def test_screenshot_raises_on_empty():
    device = Device()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with pytest.raises(ADBError, match="too small"):
            device.screenshot()


# --- Device._parse_hierarchy ---

def test_parse_hierarchy(sample_ui_xml):
    elements = Device._parse_hierarchy(sample_ui_xml)
    assert len(elements) == 6  # root + 5 children


def test_parse_hierarchy_clickable_elements(sample_ui_xml):
    elements = Device._parse_hierarchy(sample_ui_xml)
    clickable = [e for e in elements if e.clickable and e.enabled]
    assert len(clickable) == 3  # Settings title, Search btn, Network


def test_parse_hierarchy_malformed_xml():
    elements = Device._parse_hierarchy("not xml at all")
    assert elements == []


def test_parse_hierarchy_empty():
    elements = Device._parse_hierarchy("")
    assert elements == []


# --- Device.is_package_installed ---

def test_is_package_installed_true():
    device = Device()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="package:com.example.app\n", stderr="",
        )
        assert device.is_package_installed("com.example.app") is True


def test_is_package_installed_false():
    device = Device()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        assert device.is_package_installed("com.example.app") is False


def test_launch_app_raises_if_not_installed():
    device = Device()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        with pytest.raises(ADBError, match="not installed"):
            device.launch_app("com.nonexistent.app")
