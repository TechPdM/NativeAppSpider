"""Tests for Claude AI analyzer — all API calls mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nativeappspider.analyzer import (
    Analyzer,
    NavigationAction,
    ScreenAnalysis,
    _parse_json_response,
    check_api_key,
)


# --- check_api_key ---

def test_check_api_key_missing():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            check_api_key()


def test_check_api_key_present():
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        check_api_key()  # Should not raise


# --- _parse_json_response ---

def test_parse_json_plain():
    data = _parse_json_response('{"key": "value"}')
    assert data == {"key": "value"}


def test_parse_json_with_markdown_fences():
    data = _parse_json_response('```json\n{"key": "value"}\n```')
    assert data == {"key": "value"}


def test_parse_json_with_bare_fences():
    data = _parse_json_response('```\n{"key": "value"}\n```')
    assert data == {"key": "value"}


def test_parse_json_invalid():
    with pytest.raises(Exception):
        _parse_json_response("not json at all")


# --- Analyzer._parse_screen_analysis ---

def test_parse_screen_analysis_valid():
    text = '{"screen_name": "Home", "description": "Main screen", "elements": [{"label": "btn"}], "suggested_actions": []}'
    result = Analyzer._parse_screen_analysis(text)
    assert result.screen_name == "Home"
    assert result.description == "Main screen"
    assert len(result.elements) == 1
    assert result.suggested_actions == []


def test_parse_screen_analysis_with_fences():
    text = '```json\n{"screen_name": "Login", "description": "Login form"}\n```'
    result = Analyzer._parse_screen_analysis(text)
    assert result.screen_name == "Login"


def test_parse_screen_analysis_malformed():
    result = Analyzer._parse_screen_analysis("totally broken response")
    assert result.screen_name == "parse_error"
    assert result.elements == []
    assert result.suggested_actions == []


def test_parse_screen_analysis_missing_fields():
    text = '{"screen_name": "Partial"}'
    result = Analyzer._parse_screen_analysis(text)
    assert result.screen_name == "Partial"
    assert result.description == ""
    assert result.elements == []
    assert result.suggested_actions == []


def test_parse_screen_analysis_null_name():
    text = '{"screen_name": null, "description": "test"}'
    result = Analyzer._parse_screen_analysis(text)
    assert result.screen_name == "unknown"


def test_parse_screen_analysis_wrong_type_elements():
    text = '{"screen_name": "X", "elements": "not a list"}'
    result = Analyzer._parse_screen_analysis(text)
    assert result.elements == []


# --- Analyzer._parse_navigation_action ---

def test_parse_navigation_action_valid():
    text = '{"action": "tap", "x": 540, "y": 960, "reason": "explore menu"}'
    result = Analyzer._parse_navigation_action(text)
    assert result.action == "tap"
    assert result.x == 540
    assert result.y == 960
    assert result.reason == "explore menu"


def test_parse_navigation_action_back():
    text = '{"action": "back", "reason": "fully explored"}'
    result = Analyzer._parse_navigation_action(text)
    assert result.action == "back"
    assert result.x == 0
    assert result.y == 0


def test_parse_navigation_action_malformed():
    result = Analyzer._parse_navigation_action("broken json")
    assert result.action == "back"
    assert result.reason == "failed to parse AI response"


def test_parse_navigation_action_unknown_action():
    text = '{"action": "fly_away", "x": 100, "y": 200}'
    result = Analyzer._parse_navigation_action(text)
    assert result.action == "back"


def test_parse_navigation_action_missing_coordinates():
    text = '{"action": "tap"}'
    result = Analyzer._parse_navigation_action(text)
    assert result.action == "tap"
    assert result.x == 0
    assert result.y == 0


def test_parse_navigation_action_null_values():
    text = '{"action": "tap", "x": null, "y": null, "text": null, "reason": null}'
    result = Analyzer._parse_navigation_action(text)
    assert result.x == 0
    assert result.y == 0
    assert result.text == ""
    assert result.reason == ""
