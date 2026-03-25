"""Tests for crawl loop logic — Device and Analyzer fully mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from nativeappspider.analyzer import NavigationAction, ScreenAnalysis
from nativeappspider.crawler import CrawlConfig, CrawlState, Crawler, ScreenNode
from nativeappspider.hasher import screen_hash


def _make_image(color: tuple[int, int, int] = (128, 128, 128)) -> Image.Image:
    return Image.new("RGB", (100, 200), color=color)


def _make_analysis(name: str = "Test Screen") -> ScreenAnalysis:
    return ScreenAnalysis(
        screen_name=name,
        description=f"This is {name}",
        elements=[{"label": "btn", "type": "button", "purpose": "test"}],
        suggested_actions=[],
    )


def _make_action(action: str = "tap", x: int = 540, y: int = 960) -> NavigationAction:
    return NavigationAction(action=action, x=x, y=y, reason="test")


class TestCrawlState:
    def test_find_matching_screen_exact(self):
        state = CrawlState()
        img = _make_image()
        h = screen_hash(img)
        state.screens[h] = ScreenNode(h, "Home", "", "", [], "")
        assert state.find_matching_screen(h) == h

    def test_find_matching_screen_none(self):
        state = CrawlState()
        img = _make_image((255, 0, 0))
        h = screen_hash(img)
        assert state.find_matching_screen(h) is None


class TestCrawler:
    def _make_crawler(self, tmp_path: Path, max_actions: int = 5, max_screens: int = 10):
        config = CrawlConfig(
            package="com.test.app",
            max_actions=max_actions,
            max_screens=max_screens,
            output_dir=str(tmp_path),
            settle_delay=0,  # No waiting in tests
        )
        device = MagicMock()
        device.is_connected.return_value = True
        device.get_screen_size.return_value = (1080, 1920)
        device.is_package_installed.return_value = True
        device.current_activity.return_value = "com.test.app/.MainActivity"

        # Mock Analyzer construction to skip API key check
        with patch("nativeappspider.crawler.Analyzer") as MockAnalyzer:
            analyzer = MockAnalyzer.return_value
            crawler = Crawler(config, device)
            crawler.analyzer = analyzer

        return crawler, device, analyzer

    def test_crawl_terminates_on_max_actions(self, tmp_path):
        crawler, device, analyzer = self._make_crawler(tmp_path, max_actions=3)

        img = _make_image()
        device.screenshot.return_value = img
        device.get_clickable_elements.return_value = []
        analyzer.analyze_screen.return_value = _make_analysis()
        analyzer.decide_next_action.return_value = _make_action("back")

        state = crawler.crawl()
        assert state.action_count == 3

    def test_crawl_terminates_on_max_screens(self, tmp_path):
        crawler, device, analyzer = self._make_crawler(tmp_path, max_actions=100, max_screens=2)

        # Generate truly distinct images using random noise
        import numpy as np
        rng = np.random.RandomState(42)
        img_a = Image.fromarray(rng.randint(0, 128, (200, 100, 3), dtype=np.uint8))
        img_b = Image.fromarray(rng.randint(128, 256, (200, 100, 3), dtype=np.uint8))

        # Each crawl iteration calls screenshot() twice (capture + transition check)
        # Provide: img_a, img_a, img_b, img_b, ... so each pair stays consistent
        call_count = 0
        def screenshot_cycle():
            nonlocal call_count
            call_count += 1
            # First 2 calls = screen A, next 2 = screen B, then repeat A
            return img_a if ((call_count - 1) // 2) % 2 == 0 else img_b

        device.screenshot.side_effect = screenshot_cycle
        device.get_clickable_elements.return_value = []
        analyzer.analyze_screen.side_effect = [_make_analysis(f"Screen {i}") for i in range(10)]
        analyzer.decide_next_action.return_value = _make_action("tap", 100, 100)

        state = crawler.crawl()
        assert len(state.screens) >= 2

    def test_new_screen_added_to_graph(self, tmp_path):
        crawler, device, analyzer = self._make_crawler(tmp_path, max_actions=1)

        img = _make_image()
        device.screenshot.return_value = img
        device.get_clickable_elements.return_value = []
        analyzer.analyze_screen.return_value = _make_analysis("Home")
        analyzer.decide_next_action.return_value = _make_action("back")

        state = crawler.crawl()
        assert len(state.screens) == 1
        screen = list(state.screens.values())[0]
        assert screen.screen_name == "Home"

    def test_revisit_increments_count(self, tmp_path):
        crawler, device, analyzer = self._make_crawler(tmp_path, max_actions=3)

        img = _make_image()
        device.screenshot.return_value = img
        device.get_clickable_elements.return_value = []
        analyzer.analyze_screen.return_value = _make_analysis("Home")
        analyzer.decide_next_action.return_value = _make_action("back")

        state = crawler.crawl()
        screen = list(state.screens.values())[0]
        assert screen.visit_count >= 2  # First visit + at least one revisit

    def test_loop_detection_forces_back(self, tmp_path):
        crawler, device, analyzer = self._make_crawler(tmp_path, max_actions=10)

        img = _make_image()
        device.screenshot.return_value = img
        device.get_clickable_elements.return_value = []
        analyzer.analyze_screen.return_value = _make_analysis("Stuck")
        # AI always says tap — but loop detection should override to back
        analyzer.decide_next_action.return_value = _make_action("tap")

        state = crawler.crawl()
        # Should have completed without infinite loop
        assert state.action_count == 10

    def test_output_files_created(self, tmp_path):
        crawler, device, analyzer = self._make_crawler(tmp_path, max_actions=1)

        img = _make_image()
        device.screenshot.return_value = img
        device.get_clickable_elements.return_value = []
        analyzer.analyze_screen.return_value = _make_analysis("Home")
        analyzer.decide_next_action.return_value = _make_action("back")

        state = crawler.crawl()
        assert (state.output_dir / "screens.json").exists()
        assert (state.output_dir / "transitions.json").exists()
        assert (state.output_dir / "flow.mmd").exists()

        # Verify JSON is valid
        screens = json.loads((state.output_dir / "screens.json").read_text())
        assert len(screens) == 1

    def test_crawl_raises_if_device_unavailable(self, tmp_path):
        from nativeappspider.device import ADBError
        crawler, device, analyzer = self._make_crawler(tmp_path)
        device.get_screen_size.side_effect = ADBError("no device")
        with pytest.raises(ADBError):
            crawler.crawl()

    def test_screenshot_failure_doesnt_crash(self, tmp_path):
        """ADB errors during screenshot should be caught, not crash the crawl."""
        from nativeappspider.device import ADBError

        crawler, device, analyzer = self._make_crawler(tmp_path, max_actions=3)

        call_count = 0
        img = _make_image()

        def screenshot_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ADBError("screenshot failed")
            return img

        device.screenshot.side_effect = screenshot_side_effect
        device.get_clickable_elements.return_value = []
        analyzer.analyze_screen.return_value = _make_analysis()
        analyzer.decide_next_action.return_value = _make_action("back")

        # Should complete without raising
        state = crawler.crawl()
        assert state.action_count >= 1
