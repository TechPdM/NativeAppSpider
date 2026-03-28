"""Integration tests — full crawl pipeline against replay mocks.

These tests run the real Crawler code end-to-end but with scripted device
and analyzer responses. They verify that the crawler correctly builds the
state graph, saves output files, records transitions, and handles common
scenarios like revisits and backtracking.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import numpy as np
from PIL import Image

from nativeappspider.analyzer import NavigationAction, ScreenAnalysis
from nativeappspider.crawler import CrawlConfig, Crawler
from nativeappspider.device import UIElement

from nativeappspider.crawler import load_checkpoint

from .replay import AnalyzerStep, DeviceStep, ReplayAnalyzer, ReplayDevice, load_fixture


# ---------------------------------------------------------------------------
# Helpers — generate visually distinct screenshots so the hasher treats
# them as different screens.  Each uses a different random seed.
# ---------------------------------------------------------------------------

def _make_screen_image(seed: int) -> Image.Image:
    """Generate a distinct 100x200 image from a seed.

    Different seeds produce images with very different perceptual hashes,
    so the crawler's dedup logic treats them as separate screens.
    """
    rng = np.random.RandomState(seed)
    pixels = rng.randint(0, 256, (200, 100, 3), dtype=np.uint8)
    return Image.fromarray(pixels)


def _make_element(
    label: str,
    bounds: tuple[int, int, int, int] = (0, 0, 200, 80),
    package: str = "com.test.app",
) -> UIElement:
    return UIElement(
        resource_id=f"com.test.app:id/{label.lower().replace(' ', '_')}",
        class_name="android.widget.Button",
        text=label,
        content_desc="",
        package=package,
        bounds=bounds,
        clickable=True,
        scrollable=False,
        enabled=True,
    )


def _make_crawler(
    tmp_path: Path,
    device: ReplayDevice,
    analyzer: ReplayAnalyzer,
    max_actions: int = 20,
    max_screens: int = 10,
    resume_state=None,
) -> Crawler:
    """Build a Crawler wired to replay mocks, bypassing the Analyzer constructor."""
    config = CrawlConfig(
        package="com.test.app",
        max_actions=max_actions,
        max_screens=max_screens,
        output_dir=str(tmp_path),
        settle_delay=0,
    )
    # Patch Analyzer so the constructor doesn't check for an API key
    with patch("nativeappspider.crawler.Analyzer"):
        crawler = Crawler(config, device, resume_state=resume_state)
    # Swap in our replay analyzer
    crawler.analyzer = analyzer
    return crawler


# ---------------------------------------------------------------------------
# Scenario: Linear 3-screen app (Home → Settings → About → back → back)
# ---------------------------------------------------------------------------

class TestLinearThreeScreenCrawl:
    """Simulate crawling an app with three screens connected in a line:
    Home → Settings → About, then backtrack.
    """

    def _build(self, tmp_path: Path):
        img_home = _make_screen_image(seed=1)
        img_settings = _make_screen_image(seed=2)
        img_about = _make_screen_image(seed=3)

        # Script what the device returns at each step:
        # Step 0: Home screen (initial after launch)
        # Step 1: Settings screen (after tapping "Settings")
        # Step 2: About screen (after tapping "About")
        # Step 3: Settings again (after pressing back from About)
        # Step 4: Home again (after pressing back from Settings)
        device_steps = [
            DeviceStep(screenshot=img_home, clickable=[_make_element("Settings")]),
            DeviceStep(screenshot=img_settings, clickable=[_make_element("About")]),
            DeviceStep(screenshot=img_about, clickable=[]),
            DeviceStep(screenshot=img_settings, clickable=[_make_element("About")]),
            DeviceStep(screenshot=img_home, clickable=[_make_element("Settings")]),
        ]

        # Script what the analyzer returns for each new screen + action:
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis(
                    screen_name="Home",
                    description="Main home screen with navigation options",
                    elements=[{"label": "Settings", "type": "button", "purpose": "open settings"}],
                    suggested_actions=[],
                ),
                action=NavigationAction(action="tap", x=100, y=40, reason="explore Settings"),
            ),
            AnalyzerStep(
                analysis=ScreenAnalysis(
                    screen_name="Settings",
                    description="App settings and preferences",
                    elements=[{"label": "About", "type": "button", "purpose": "show about page"}],
                    suggested_actions=[],
                ),
                action=NavigationAction(action="tap", x=100, y=40, reason="explore About"),
            ),
            AnalyzerStep(
                analysis=ScreenAnalysis(
                    screen_name="About",
                    description="About this application",
                    elements=[],
                    suggested_actions=[],
                ),
                action=NavigationAction(action="back", reason="no more elements to explore"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(
            analyzer_steps,
            fallback_action=NavigationAction(action="back", reason="backtracking"),
        )
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=5)
        return crawler, device

    def test_discovers_three_screens(self, tmp_path):
        crawler, _ = self._build(tmp_path)
        state = crawler.crawl()
        assert len(state.screens) == 3

    def test_screen_names(self, tmp_path):
        crawler, _ = self._build(tmp_path)
        state = crawler.crawl()
        names = {s.screen_name for s in state.screens.values()}
        assert names == {"Home", "Settings", "About"}

    def test_graph_has_forward_transitions(self, tmp_path):
        crawler, _ = self._build(tmp_path)
        state = crawler.crawl()

        # Should have edges Home→Settings and Settings→About
        assert state.graph.number_of_edges() >= 2

        # Verify edge data includes action types
        edge_actions = [d["action"] for _, _, d in state.graph.edges(data=True)]
        assert "tap" in edge_actions

    def test_output_files_valid(self, tmp_path):
        crawler, _ = self._build(tmp_path)
        state = crawler.crawl()

        # All required output files exist
        assert (state.output_dir / "screens.json").exists()
        assert (state.output_dir / "transitions.json").exists()
        assert (state.output_dir / "flow.mmd").exists()

        # JSON files are parseable
        screens = json.loads((state.output_dir / "screens.json").read_text())
        transitions = json.loads((state.output_dir / "transitions.json").read_text())
        assert len(screens) == 3
        assert len(transitions) >= 2

    def test_screenshots_saved(self, tmp_path):
        crawler, _ = self._build(tmp_path)
        state = crawler.crawl()

        ss_dir = state.output_dir / "screenshots"
        pngs = list(ss_dir.glob("*.png"))
        assert len(pngs) == 3

    def test_mermaid_diagram(self, tmp_path):
        crawler, _ = self._build(tmp_path)
        state = crawler.crawl()

        mmd = (state.output_dir / "flow.mmd").read_text()
        assert mmd.startswith("graph TD")
        # All three screen names should appear in the diagram
        assert "Home" in mmd
        assert "Settings" in mmd
        assert "About" in mmd

    def test_device_received_actions(self, tmp_path):
        crawler, device = self._build(tmp_path)
        crawler.crawl()

        # The device should have received the app launch + navigation actions
        assert "launch:com.test.app" in device.actions_performed
        assert any(a.startswith("tap:") for a in device.actions_performed)


# ---------------------------------------------------------------------------
# Scenario: Single-screen app (only one screen, crawler backtracks repeatedly)
# ---------------------------------------------------------------------------

class TestSingleScreenCrawl:
    """An app with just one screen — the crawler should not hang."""

    def test_terminates_cleanly(self, tmp_path):
        img = _make_screen_image(seed=10)

        device_steps = [
            DeviceStep(screenshot=img, clickable=[_make_element("Button")]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis(
                    screen_name="Only Screen",
                    description="The only screen in the app",
                    elements=[{"label": "Button", "type": "button", "purpose": "does nothing"}],
                    suggested_actions=[],
                ),
                action=NavigationAction(action="tap", x=100, y=40, reason="try the button"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=10)

        state = crawler.crawl()
        assert len(state.screens) == 1
        assert state.action_count == 10  # Should exhaust the action budget


# ---------------------------------------------------------------------------
# Scenario: App escape and relaunch
# ---------------------------------------------------------------------------

class TestAppEscapeRelaunch:
    """Simulates leaving the target app (e.g. opening a browser link),
    triggering the relaunch logic.
    """

    def test_relaunches_after_leaving_app(self, tmp_path):
        img_home = _make_screen_image(seed=20)
        img_external = _make_screen_image(seed=21)

        device_steps = [
            # Step 0: Home screen
            DeviceStep(screenshot=img_home, clickable=[_make_element("Link")]),
            # Step 1: Left the app (browser opened)
            DeviceStep(
                screenshot=img_external,
                clickable=[],
                activity="com.android.browser/.BrowserActivity",
            ),
            # Step 2: Back to home after relaunch
            DeviceStep(screenshot=img_home, clickable=[_make_element("Link")]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis(
                    screen_name="Home",
                    description="Home screen",
                    elements=[],
                    suggested_actions=[],
                ),
                action=NavigationAction(action="tap", x=100, y=40, reason="tap the link"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(
            analyzer_steps,
            fallback_action=NavigationAction(action="back", reason="backtrack"),
        )
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=5)

        state = crawler.crawl()

        # The crawler should have re-launched the app
        launch_actions = [a for a in device.actions_performed if a.startswith("launch:")]
        assert len(launch_actions) >= 2  # Initial launch + at least one relaunch


# ---------------------------------------------------------------------------
# Scenario: Revisit counting and transition recording
# ---------------------------------------------------------------------------

class TestRevisitAndTransitions:
    """Two screens where the crawler bounces back and forth, verifying
    that revisit counts increment and transitions are recorded.
    """

    def test_revisits_counted_and_transitions_recorded(self, tmp_path):
        img_a = _make_screen_image(seed=30)
        img_b = _make_screen_image(seed=31)

        # Script: A → B → A → B → A
        device_steps = [
            DeviceStep(screenshot=img_a, clickable=[_make_element("Go B")]),
            DeviceStep(screenshot=img_b, clickable=[_make_element("Go A")]),
            DeviceStep(screenshot=img_a, clickable=[_make_element("Go B")]),
            DeviceStep(screenshot=img_b, clickable=[_make_element("Go A")]),
            DeviceStep(screenshot=img_a, clickable=[_make_element("Go B")]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Screen A", "First screen", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="go to B"),
            ),
            AnalyzerStep(
                analysis=ScreenAnalysis("Screen B", "Second screen", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="go to A"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(
            analyzer_steps,
            fallback_action=NavigationAction(action="tap", x=100, y=40, reason="keep bouncing"),
        )
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=5)

        state = crawler.crawl()

        # Should discover exactly 2 screens
        assert len(state.screens) == 2

        # Revisit counts should be > 1 for at least one screen
        visit_counts = [s.visit_count for s in state.screens.values()]
        assert max(visit_counts) >= 2

        # Graph should have edges in both directions (A→B and B→A)
        assert state.graph.number_of_edges() >= 2


# ---------------------------------------------------------------------------
# Scenario: Max depth triggers backtracking
# ---------------------------------------------------------------------------

class TestMaxDepthBacktracking:
    """A deep chain of screens — the crawler should backtrack once it
    hits the max_depth limit instead of going deeper forever.
    """

    def test_backtracks_at_max_depth(self, tmp_path):
        # 5 distinct screen images: S0 → S1 → S2 → S3 → S4
        images = [_make_screen_image(seed=40 + i) for i in range(5)]

        # Script the device to show a forward chain, then return to
        # previous screens when the crawler presses back:
        #   Step 0: S0 (initial)
        #   Step 1: S1 (after tap)
        #   Step 2: S2 (after tap)
        #   Step 3: S3 (after tap — depth limit will trigger here)
        #   Step 4: S2 (after back from S3)
        #   Step 5: S1 (after back from S2)
        #   Step 6: S0 (after back from S1)
        device_steps = [
            DeviceStep(screenshot=images[0], clickable=[_make_element("Next 0")]),
            DeviceStep(screenshot=images[1], clickable=[_make_element("Next 1")]),
            DeviceStep(screenshot=images[2], clickable=[_make_element("Next 2")]),
            DeviceStep(screenshot=images[3], clickable=[_make_element("Next 3")]),
            # After backs — return to previously-seen screens
            DeviceStep(screenshot=images[2], clickable=[_make_element("Next 2")]),
            DeviceStep(screenshot=images[1], clickable=[_make_element("Next 1")]),
            DeviceStep(screenshot=images[0], clickable=[_make_element("Next 0")]),
        ]

        # Analyzer always says "go deeper" — only the depth limit should stop it
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis(f"Screen {i}", f"Screen at depth {i}", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason=f"go deeper to {i+1}"),
            )
            for i in range(5)
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(
            analyzer_steps,
            # Fallback also says tap — so only the depth heuristic causes backs
            fallback_action=NavigationAction(action="tap", x=100, y=40, reason="keep going"),
        )
        # max_depth=2 means the path [S0, S1, S2] is OK but adding S3 triggers back
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=8, max_screens=20)
        crawler.config.max_depth = 2

        state = crawler.crawl()

        # Should discover exactly 4 screens (S0, S1, S2, S3) — S3 is seen
        # but then depth limit forces immediate backtracking
        assert len(state.screens) <= 4, (
            f"Discovered {len(state.screens)} screens — depth limit of 2 should have "
            f"prevented going beyond S3"
        )
        # The crawler must have pressed back (depth limit triggered)
        assert "back" in device.actions_performed


# ---------------------------------------------------------------------------
# Scenario: Analysis failure falls back to minimal screen recording
# ---------------------------------------------------------------------------

class TestAnalysisFailureFallback:
    """When analyze_screen() throws an error, the crawler should still
    record the screen (with minimal info) and continue crawling.
    """

    def test_records_screen_despite_analysis_error(self, tmp_path):
        img_home = _make_screen_image(seed=50)
        img_broken = _make_screen_image(seed=51)

        device_steps = [
            DeviceStep(screenshot=img_home, clickable=[_make_element("Go")]),
            # This screen's analysis will fail
            DeviceStep(screenshot=img_broken, clickable=[]),
        ]

        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Home screen", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="explore"),
            ),
            # Second screen analysis throws an error
            AnalyzerStep(
                analysis=ScreenAnalysis("unused", "", [], []),
                action=NavigationAction(action="back", reason="go back"),
                analyze_error=RuntimeError("API connection failed"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=3)

        state = crawler.crawl()

        # Both screens should be recorded — the broken one with a fallback name
        assert len(state.screens) == 2
        names = [s.screen_name for s in state.screens.values()]
        assert "Home" in names
        # The failed screen gets a generated name like "screen_2"
        fallback_names = [n for n in names if n.startswith("screen_")]
        assert len(fallback_names) == 1


# ---------------------------------------------------------------------------
# Scenario: Duplicate screen names are deduplicated in output
# ---------------------------------------------------------------------------

class TestDuplicateScreenNameDedup:
    """When Claude gives the same name to two visually different screens,
    name-based dedup treats the second as a revisit of the first.
    """

    def test_deduplicates_same_named_screens(self, tmp_path):
        img_a = _make_screen_image(seed=60)
        img_b = _make_screen_image(seed=61)

        device_steps = [
            DeviceStep(screenshot=img_a, clickable=[_make_element("Go")]),
            DeviceStep(screenshot=img_b, clickable=[]),
        ]

        # Both screens get named "Settings" by the analyzer
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Settings", "Network settings", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="explore"),
            ),
            AnalyzerStep(
                analysis=ScreenAnalysis("Settings", "Display settings", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=2)

        state = crawler.crawl()

        # Name-based dedup merges the second "Settings" into the first,
        # so only one unique screen is recorded with an incremented visit count
        settings_screens = [
            s for s in state.screens.values() if s.screen_name == "Settings"
        ]
        assert len(settings_screens) == 1
        assert settings_screens[0].visit_count >= 2


# ---------------------------------------------------------------------------
# Scenario: Swipe actions are dispatched correctly
# ---------------------------------------------------------------------------

class TestSwipeActions:
    """Verify that swipe_up and swipe_down actions are translated into
    correct device swipe calls with the right coordinates.
    """

    def test_swipe_up_coordinates(self, tmp_path):
        img = _make_screen_image(seed=70)

        device_steps = [
            DeviceStep(screenshot=img, clickable=[]),
            DeviceStep(screenshot=img, clickable=[]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Scrollable", "A long list", [], []),
                action=NavigationAction(action="swipe_up", reason="scroll to see more"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=2)

        crawler.crawl()

        # Should have a swipe action with correct coordinates
        # Screen is 1080x1920, so swipe from (540, 1440) to (540, 480)
        swipes = [a for a in device.actions_performed if a.startswith("swipe:")]
        assert len(swipes) >= 1
        assert "540,1440->540,480" in swipes[0]

    def test_swipe_down_coordinates(self, tmp_path):
        img = _make_screen_image(seed=71)

        device_steps = [
            DeviceStep(screenshot=img, clickable=[]),
            DeviceStep(screenshot=img, clickable=[]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Scrollable", "A long list", [], []),
                action=NavigationAction(action="swipe_down", reason="scroll back up"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=2)

        crawler.crawl()

        swipes = [a for a in device.actions_performed if a.startswith("swipe:")]
        assert len(swipes) >= 1
        # Swipe from (540, 480) to (540, 1440) — opposite of swipe_up
        assert "540,480->540,1440" in swipes[0]


# ---------------------------------------------------------------------------
# Scenario: Text input action
# ---------------------------------------------------------------------------

class TestTextInputAction:
    """Verify that 'type' actions pass the text through to the device."""

    def test_text_input_dispatched(self, tmp_path):
        img = _make_screen_image(seed=80)

        device_steps = [
            DeviceStep(screenshot=img, clickable=[_make_element("Search")]),
            DeviceStep(screenshot=img, clickable=[]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Search", "Search screen", [], []),
                action=NavigationAction(
                    action="type", text="hello world", reason="type a search query"
                ),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=2)

        crawler.crawl()

        type_actions = [a for a in device.actions_performed if a.startswith("type:")]
        assert len(type_actions) >= 1
        assert "type:hello world" in type_actions


# ---------------------------------------------------------------------------
# Scenario: Screen action history is tracked per-screen
# ---------------------------------------------------------------------------

class TestScreenActionHistory:
    """Verify that actions are tracked per-screen and accumulate across
    revisits, so the analyzer receives the history of what was already
    tried on each screen.
    """

    def test_action_history_accumulates(self, tmp_path):
        img = _make_screen_image(seed=90)

        # Same screen every step — multiple buttons so the per-element
        # detector doesn't force back before actions accumulate
        btn_a = _make_element("Btn A", bounds=(0, 0, 200, 80))
        btn_b = _make_element("Btn B", bounds=(0, 100, 200, 180))
        btn_c = _make_element("Btn C", bounds=(0, 200, 200, 280))
        device_steps = [
            DeviceStep(screenshot=img, clickable=[btn_a, btn_b, btn_c]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Home screen", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="try A"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(
            analyzer_steps,
            fallback_action=NavigationAction(action="tap", x=100, y=140, reason="try B"),
        )
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=4)

        state = crawler.crawl()

        # The single screen should have accumulated multiple action records
        screen_id = list(state.screens.keys())[0]
        actions = state.screen_actions.get(screen_id, [])
        assert len(actions) >= 2  # At least the initial tap + fallback taps


# ---------------------------------------------------------------------------
# Scenario: Report generation from crawl output
# ---------------------------------------------------------------------------

class TestReportGeneration:
    """Verify that the HTML report can be generated from crawl output
    and contains the expected content.
    """

    def test_report_html_contains_all_screens(self, tmp_path):
        from nativeappspider.reporter import generate_html_report

        img_a = _make_screen_image(seed=100)
        img_b = _make_screen_image(seed=101)

        device_steps = [
            DeviceStep(screenshot=img_a, clickable=[_make_element("Next")]),
            DeviceStep(screenshot=img_b, clickable=[]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Login", "User login form", [
                    {"label": "Username", "type": "input", "purpose": "enter username"},
                    {"label": "Password", "type": "input", "purpose": "enter password"},
                ], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="submit"),
            ),
            AnalyzerStep(
                analysis=ScreenAnalysis("Dashboard", "Main dashboard after login", [
                    {"label": "Logout", "type": "button", "purpose": "sign out"},
                ], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=3)

        state = crawler.crawl()

        # Generate the report from the crawl output
        report_path = generate_html_report(state.output_dir)
        assert report_path.exists()

        html = report_path.read_text()

        # Report should contain both screen names
        assert "Login" in html
        assert "Dashboard" in html

        # Report should contain element details
        assert "Username" in html
        assert "Logout" in html

        # Report should have the Mermaid diagram
        assert "graph TD" in html

        # Report should show transition count
        assert "Transitions" in html


# ---------------------------------------------------------------------------
# Scenario: Per-element loop detection
# ---------------------------------------------------------------------------

class TestPreciseLoopDetection:
    """Verify that the crawler forces 'back' when all clickable elements
    on a screen have been tapped, without waiting for the consecutive_known
    safety net or consulting Claude.
    """

    def test_forces_back_when_all_elements_tried(self, tmp_path):
        """Two elements on a screen — after both are tapped, the crawler
        should back out immediately on the next visit.
        """
        img = _make_screen_image(seed=110)

        btn_a = _make_element("Button A", bounds=(0, 0, 200, 80))
        btn_b = _make_element("Button B", bounds=(0, 100, 200, 180))

        # Same screen every step, with both buttons visible
        device_steps = [
            DeviceStep(screenshot=img, clickable=[btn_a, btn_b]),
        ]

        # Analyzer returns taps on each button in order
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Home screen", [], []),
                # Tap center of Button A (100, 40)
                action=NavigationAction(action="tap", x=100, y=40, reason="try A"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(
            analyzer_steps,
            # Fallback taps center of Button B (100, 140)
            fallback_action=NavigationAction(action="tap", x=100, y=140, reason="try B"),
        )
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=5)

        state = crawler.crawl()

        # After tapping both buttons, the third visit should force back
        # with "all elements explored" reason
        assert "back" in device.actions_performed

        # The screen should have both elements recorded as tapped
        screen_id = list(state.screens.keys())[0]
        tapped = state.screen_tapped_elements.get(screen_id, set())
        assert len(tapped) == 2

    def test_does_not_force_back_with_untried_elements(self, tmp_path):
        """Three elements on screen, only one tapped so far — should NOT
        force back, should consult Claude.
        """
        img = _make_screen_image(seed=111)

        btn_a = _make_element("Button A", bounds=(0, 0, 200, 80))
        btn_b = _make_element("Button B", bounds=(0, 100, 200, 180))
        btn_c = _make_element("Button C", bounds=(0, 200, 200, 280))

        device_steps = [
            DeviceStep(screenshot=img, clickable=[btn_a, btn_b, btn_c]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Home screen", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="try A"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(
            analyzer_steps,
            # Fallback also taps A — but since B and C are untried, Claude
            # should still be consulted (not forced back)
            fallback_action=NavigationAction(action="tap", x=100, y=40, reason="try A again"),
        )
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=3)

        state = crawler.crawl()

        # Should NOT have any "back" actions — only taps, because untried
        # elements exist. Filter out launch/force_stop setup actions.
        nav_actions = [
            a for a in device.actions_performed
            if not a.startswith(("launch:", "force_stop:"))
        ]
        assert all(a.startswith("tap:") for a in nav_actions)

    def test_safety_net_still_works(self, tmp_path):
        """Even if per-element tracking doesn't trigger (e.g. empty clickable
        list on revisits), the consecutive_known > 10 safety net should
        eventually force back.
        """
        img = _make_screen_image(seed=112)

        # Screen with no clickable elements — per-element check has nothing
        # to compare, so the safety net (consecutive_known) must handle it
        device_steps = [
            DeviceStep(screenshot=img, clickable=[]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Empty", "No buttons", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="blind tap"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(
            analyzer_steps,
            fallback_action=NavigationAction(action="tap", x=100, y=40, reason="keep trying"),
        )
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=15)

        state = crawler.crawl()

        # The safety net should have triggered "back" at some point
        assert "back" in device.actions_performed


# ---------------------------------------------------------------------------
# Scenario: System dialog detection and auto-dismissal
# ---------------------------------------------------------------------------

def _make_system_element(
    label: str,
    package: str = "com.android.permissioncontroller",
    clickable: bool = True,
    bounds: tuple[int, int, int, int] = (200, 800, 880, 900),
) -> UIElement:
    """Create a UIElement that looks like part of a system dialog."""
    return UIElement(
        resource_id=f"{package}:id/{label.lower().replace(' ', '_')}",
        class_name="android.widget.Button",
        text=label,
        content_desc="",
        package=package,
        bounds=bounds,
        clickable=clickable,
        scrollable=False,
        enabled=True,
    )


class TestSystemDialogDismissal:
    """Verify that system dialog overlays (permissions, ANR, etc.) are
    detected and auto-dismissed without being recorded as app screens.
    """

    def test_permission_dialog_auto_dismissed(self, tmp_path):
        """A permission dialog with an 'Allow' button should be tapped
        and the dialog should NOT be recorded as a screen.
        """
        img_dialog = _make_screen_image(seed=120)
        img_home = _make_screen_image(seed=121)

        allow_btn = _make_system_element("Allow", bounds=(500, 800, 800, 900))

        device_steps = [
            # Step 0: Permission dialog is showing
            DeviceStep(
                screenshot=img_dialog,
                clickable=[],
                ui_hierarchy=[
                    _make_system_element("Deny", bounds=(200, 800, 500, 900)),
                    allow_btn,
                ],
            ),
            # Step 1: After dismissing, the real home screen appears
            DeviceStep(
                screenshot=img_home,
                clickable=[_make_element("Settings")],
            ),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Home screen", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=3)

        state = crawler.crawl()

        # The dialog should NOT have been recorded as a screen
        assert len(state.screens) == 1
        assert list(state.screens.values())[0].screen_name == "Home"

        # A dismiss button should have been tapped (either "Deny" or "Allow")
        tap_actions = [a for a in device.actions_performed if a.startswith("tap:")]
        assert len(tap_actions) >= 1

    def test_dialog_dismissed_via_back(self, tmp_path):
        """A system dialog with no recognizable dismiss button should be
        dismissed by pressing back.
        """
        img_dialog = _make_screen_image(seed=130)
        img_home = _make_screen_image(seed=131)

        # Dialog with only a non-standard label
        weird_btn = _make_system_element("Some random text", package="android")

        device_steps = [
            DeviceStep(
                screenshot=img_dialog,
                clickable=[],
                ui_hierarchy=[weird_btn],
            ),
            DeviceStep(
                screenshot=img_home,
                clickable=[_make_element("Menu")],
            ),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Home screen", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=3)

        state = crawler.crawl()

        # Dialog should not be a screen
        assert len(state.screens) == 1

        # Back should have been pressed to dismiss
        # (first "back" is the dialog dismiss, second might be from navigation)
        assert "back" in device.actions_performed

    def test_normal_screen_not_treated_as_dialog(self, tmp_path):
        """An app screen with package='com.test.app' should be processed
        normally, not treated as a system dialog.
        """
        img = _make_screen_image(seed=140)

        app_btn = _make_element("Settings")

        device_steps = [
            DeviceStep(
                screenshot=img,
                clickable=[app_btn],
                # Full hierarchy also has package="com.test.app" — not a system dialog
                ui_hierarchy=[app_btn],
            ),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Home screen", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=2)

        state = crawler.crawl()

        # Should be recorded as a normal screen
        assert len(state.screens) == 1
        assert list(state.screens.values())[0].screen_name == "Home"


# ---------------------------------------------------------------------------
# Scenario: Scrollable container element discovery
# ---------------------------------------------------------------------------

def _make_scrollable_container(bounds: tuple[int, int, int, int] = (0, 96, 1080, 1920)) -> UIElement:
    """Create a scrollable container element."""
    return UIElement(
        resource_id="com.test.app:id/scroll_view",
        class_name="android.widget.ScrollView",
        text="",
        content_desc="",
        package="com.test.app",
        bounds=bounds,
        clickable=False,
        scrollable=True,
        enabled=True,
    )


class TestScrollableContainerDiscovery:
    """Verify that the crawler scrolls through scrollable containers
    to discover off-screen elements on new screens.
    """

    def test_scroll_reveals_new_elements(self, tmp_path):
        """A scrollable container with off-screen elements — scrolling
        should reveal them and include them in the screen's element list.
        """
        img = _make_screen_image(seed=150)

        btn_a = _make_element("Visible A", bounds=(0, 200, 500, 300))
        btn_b = _make_element("Visible B", bounds=(0, 400, 500, 500))
        # These appear after scrolling
        btn_c = _make_element("Hidden C", bounds=(0, 200, 500, 300))
        btn_d = _make_element("Hidden D", bounds=(0, 400, 500, 500))

        container = _make_scrollable_container()

        # Step 0: Initial screen with 2 visible buttons + scrollable container
        # Step 1: After scroll, device returns 2 new buttons
        # Step 2: After second scroll, no new buttons (same as step 1)
        device_steps = [
            DeviceStep(
                screenshot=img,
                clickable=[btn_a, btn_b],
                ui_hierarchy=[container, btn_a, btn_b],
            ),
            # After first scroll: new elements appear
            DeviceStep(
                screenshot=img,
                clickable=[btn_c, btn_d],
                ui_hierarchy=[container, btn_c, btn_d],
            ),
            # After second scroll: same elements (no new ones → stop)
            DeviceStep(
                screenshot=img,
                clickable=[btn_c, btn_d],
                ui_hierarchy=[container, btn_c, btn_d],
            ),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Long List", "A scrollable list", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=3)

        state = crawler.crawl()

        # Should have performed swipe actions within the container
        swipes = [a for a in device.actions_performed if a.startswith("swipe:")]
        assert len(swipes) >= 1

        # The swipe should be within the container bounds (x center = 540)
        assert "540," in swipes[0]

    def test_scroll_stops_when_no_new_elements(self, tmp_path):
        """If scrolling reveals no new elements, the crawler should stop
        scrolling after one attempt.
        """
        img = _make_screen_image(seed=160)

        btn_a = _make_element("Button", bounds=(0, 200, 500, 300))
        container = _make_scrollable_container()

        # Same elements before and after scroll
        device_steps = [
            DeviceStep(
                screenshot=img,
                clickable=[btn_a],
                ui_hierarchy=[container, btn_a],
            ),
            # After scroll: same element, no new ones
            DeviceStep(
                screenshot=img,
                clickable=[btn_a],
            ),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Short List", "Nothing to scroll", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=3)

        state = crawler.crawl()

        # Should have tried exactly 1 scroll, then stopped
        swipes = [a for a in device.actions_performed if a.startswith("swipe:")]
        assert len(swipes) == 1

    def test_no_scroll_when_no_scrollable_containers(self, tmp_path):
        """A screen with no scrollable elements should not trigger any
        scroll-related swipe actions during element discovery.
        """
        img = _make_screen_image(seed=170)

        device_steps = [
            DeviceStep(
                screenshot=img,
                clickable=[_make_element("Button")],
                # No scrollable elements in hierarchy
                ui_hierarchy=[_make_element("Button")],
            ),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Static", "No scrolling", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=2)

        state = crawler.crawl()

        # No swipe actions should have occurred during discovery
        swipes = [a for a in device.actions_performed if a.startswith("swipe:")]
        assert len(swipes) == 0


# ---------------------------------------------------------------------------
# Scenario: --focus flag navigates to target then explores
# ---------------------------------------------------------------------------

class TestFocusNavigation:
    """Verify that --focus biases navigation toward a target screen,
    then switches to normal exploration once reached.
    """

    def test_focus_navigates_to_target(self, tmp_path):
        """Crawl with focus='map'. The crawler passes through Splash and
        Menu, then reaches Map and marks focus_reached.
        """
        img_splash = _make_screen_image(seed=200)
        img_menu = _make_screen_image(seed=201)
        img_map = _make_screen_image(seed=202)
        img_detail = _make_screen_image(seed=203)

        device_steps = [
            DeviceStep(screenshot=img_splash, clickable=[_make_element("Continue")]),
            DeviceStep(screenshot=img_menu, clickable=[_make_element("Map"), _make_element("Settings")]),
            DeviceStep(screenshot=img_map, clickable=[_make_element("Pin A"), _make_element("Filter")]),
            DeviceStep(screenshot=img_detail, clickable=[]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Splash", "Welcome splash screen", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="dismiss splash"),
            ),
            AnalyzerStep(
                analysis=ScreenAnalysis("Main Menu", "App main menu", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="go to map"),
            ),
            AnalyzerStep(
                analysis=ScreenAnalysis("Map View", "Interactive map showing locations", [], [],
                                        matches_focus_target=True),
                action=NavigationAction(action="tap", x=100, y=40, reason="tap a pin"),
            ),
            AnalyzerStep(
                analysis=ScreenAnalysis("Location Detail", "Details about a location", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=5)
        crawler.config.focus_screen = "map"

        state = crawler.crawl()

        # Focus should have been reached at "Map View"
        assert state.focus_reached is True
        # All screens along the way should still be recorded
        names = {s.screen_name for s in state.screens.values()}
        assert "Map View" in names
        assert "Splash" in names

    def test_focus_stops_biasing_after_reached(self, tmp_path):
        """Once the focus screen is reached, the analyzer should NOT
        receive focus_screen anymore — normal exploration resumes.
        """
        img_a = _make_screen_image(seed=210)
        img_target = _make_screen_image(seed=211)
        img_c = _make_screen_image(seed=212)

        device_steps = [
            DeviceStep(screenshot=img_a, clickable=[_make_element("Go")]),
            DeviceStep(screenshot=img_target, clickable=[_make_element("Explore")]),
            DeviceStep(screenshot=img_c, clickable=[]),
        ]

        received_focus = []

        class SpyAnalyzer:
            """Tracks what focus_screen values are passed to each call."""
            def __init__(self):
                self._step = 0

            def analyze_screen(self, screenshot, **kwargs):
                received_focus.append(("analyze", kwargs.get("focus_screen")))
                names = ["Home", "Settings Page", "Sub Settings"]
                name = names[self._step] if self._step < len(names) else "Extra"
                # "Settings Page" is the focus target
                is_target = (name == "Settings Page")
                return ScreenAnalysis(name, f"Screen {self._step}", [], [],
                                      matches_focus_target=is_target)

            def decide_next_action(self, screenshot, clickable_elements, visited_screens, **kwargs):
                received_focus.append(("decide", kwargs.get("focus_screen")))
                action = NavigationAction(action="tap", x=100, y=40, reason="explore")
                self._step += 1
                return action

        device = ReplayDevice(device_steps)
        crawler = _make_crawler(tmp_path, device, SpyAnalyzer(), max_actions=3)
        crawler.config.focus_screen = "settings"

        state = crawler.crawl()

        # First screen ("Home") — focus not yet reached, should pass "settings"
        assert received_focus[0] == ("analyze", "settings")
        assert received_focus[1] == ("decide", "settings")

        # Second screen ("Settings Page") — focus matched! analyze gets it,
        # but decide should NOT (focus_reached flipped after analyze)
        assert received_focus[2] == ("analyze", "settings")
        assert received_focus[3] == ("decide", None)

        # Third screen — focus already reached, neither gets it
        assert received_focus[4] == ("analyze", None)

    def test_focus_none_behaves_normally(self, tmp_path):
        """Without --focus, the crawler explores normally without any
        focus-related state changes.
        """
        img = _make_screen_image(seed=220)

        device_steps = [
            DeviceStep(screenshot=img, clickable=[_make_element("Btn")]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Home screen", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=2)
        # No focus_screen set (default None)

        state = crawler.crawl()

        assert state.focus_reached is False
        assert len(state.screens) == 1


# ---------------------------------------------------------------------------
# Scenario: Replay from real crawl fixture data (Android Settings)
# ---------------------------------------------------------------------------

SETTINGS_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "settings"


def _make_fixture_crawler(tmp_path: Path, fixture_dir: Path) -> tuple[Crawler, ReplayDevice]:
    """Build a Crawler from fixture data, bypassing the Analyzer constructor."""
    device, analyzer, config = load_fixture(fixture_dir)
    config.output_dir = str(tmp_path)
    config.settle_delay = 0

    crawler = Crawler.__new__(Crawler)
    crawler.config = config
    crawler.device = device
    crawler.analyzer = analyzer
    crawler.state = __import__(
        "nativeappspider.crawler", fromlist=["CrawlState"]
    ).CrawlState()
    crawler._record = False
    crawler._recorder = None
    return crawler, device


class TestSettingsFixture:
    """Replay a real Android Settings crawl from captured fixture data.

    Uses actual screenshots and analysis results recorded during a live
    crawl of the built-in Settings app (com.android.settings).
    """

    @staticmethod
    def _fixture_exists():
        return (SETTINGS_FIXTURE_DIR / "scenario.json").exists()

    def test_fixture_crawl_discovers_screens(self, tmp_path):
        if not self._fixture_exists():
            pytest.skip("Settings fixture not found")

        crawler, _ = _make_fixture_crawler(tmp_path, SETTINGS_FIXTURE_DIR)
        state = crawler.crawl()

        assert len(state.screens) >= 3

        assert (state.output_dir / "screens.json").exists()
        assert (state.output_dir / "transitions.json").exists()
        assert (state.output_dir / "flow.mmd").exists()

    def test_fixture_crawl_produces_valid_graph(self, tmp_path):
        if not self._fixture_exists():
            pytest.skip("Settings fixture not found")

        crawler, _ = _make_fixture_crawler(tmp_path, SETTINGS_FIXTURE_DIR)
        state = crawler.crawl()

        assert state.graph.number_of_nodes() >= 1
        for screen in state.screens.values():
            assert screen.screen_name
            assert isinstance(screen.screen_name, str)

    def test_fixture_crawl_records_transitions(self, tmp_path):
        if not self._fixture_exists():
            pytest.skip("Settings fixture not found")

        crawler, _ = _make_fixture_crawler(tmp_path, SETTINGS_FIXTURE_DIR)
        state = crawler.crawl()

        # With multiple screens there should be at least one transition
        transitions = json.loads((state.output_dir / "transitions.json").read_text())
        assert len(transitions) >= 1
        # Each transition should have from/to screen names
        for t in transitions:
            assert t["from"]
            assert t["to"]


# ---------------------------------------------------------------------------
# Scenario: Crawl resume/continuation
# ---------------------------------------------------------------------------

class TestCrawlResume:
    """Test resuming a crawl from a checkpoint.

    First crawl discovers 2 screens then stops (budget hit).
    Second crawl loads the checkpoint and discovers additional screens.
    """

    def test_resume_continues_from_checkpoint(self, tmp_path):
        # --- Phase 1: initial crawl with budget of 2 screens ---
        img_a = _make_screen_image(seed=200)
        img_b = _make_screen_image(seed=201)
        img_c = _make_screen_image(seed=202)

        device_steps_1 = [
            DeviceStep(screenshot=img_a, clickable=[_make_element("Settings")]),
            DeviceStep(screenshot=img_b, clickable=[_make_element("WiFi")]),
        ]
        analyzer_steps_1 = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Main screen", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="go to settings"),
            ),
            AnalyzerStep(
                analysis=ScreenAnalysis("Settings", "App settings", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="go to wifi"),
            ),
        ]

        device_1 = ReplayDevice(device_steps_1)
        analyzer_1 = ReplayAnalyzer(analyzer_steps_1)
        crawler_1 = _make_crawler(tmp_path, device_1, analyzer_1, max_screens=2, max_actions=5)
        state_1 = crawler_1.crawl()

        assert len(state_1.screens) == 2
        # Checkpoint should have been saved
        assert (state_1.output_dir / "crawl_state.json").exists()
        assert (state_1.output_dir / "screens.json").exists()

        # --- Phase 2: resume from checkpoint with higher budget ---
        resume_state, resume_config = load_checkpoint(state_1.output_dir)

        assert len(resume_state.screens) == 2
        assert resume_state.action_count > 0

        device_steps_2 = [
            # First screenshot after resume — lands on a known screen
            DeviceStep(screenshot=img_b, clickable=[_make_element("About")]),
            # Then discovers a new screen
            DeviceStep(screenshot=img_c, clickable=[]),
        ]
        analyzer_steps_2 = [
            # First step is consumed by decide_next_action on the revisited screen
            AnalyzerStep(
                analysis=ScreenAnalysis("_unused", "", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="explore about"),
            ),
            # Second step is the actual new screen analysis
            AnalyzerStep(
                analysis=ScreenAnalysis("About", "About page", [], []),
                action=NavigationAction(action="back", reason="done"),
            ),
        ]

        device_2 = ReplayDevice(device_steps_2)
        analyzer_2 = ReplayAnalyzer(analyzer_steps_2)
        crawler_2 = _make_crawler(
            tmp_path, device_2, analyzer_2,
            max_screens=5, max_actions=10,
            resume_state=resume_state,
        )
        # Point output_dir to the same directory
        crawler_2.state.output_dir = state_1.output_dir
        state_2 = crawler_2.crawl()

        # Should now have 3 screens (2 original + 1 new)
        assert len(state_2.screens) == 3
        names = {s.screen_name for s in state_2.screens.values()}
        assert "Home" in names
        assert "Settings" in names
        assert "About" in names

    def test_checkpoint_preserves_tapped_elements(self, tmp_path):
        """Verify that screen_tapped_elements survives save/load cycle."""
        img = _make_screen_image(seed=210)
        btn = _make_element("Button", bounds=(10, 20, 200, 80))

        device_steps = [
            DeviceStep(screenshot=img, clickable=[btn]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Screen", "Test", [], []),
                action=NavigationAction(action="tap", x=100, y=50, reason="tap"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(analyzer_steps)
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=1)
        state = crawler.crawl()

        assert (state.output_dir / "crawl_state.json").exists()

        # Load checkpoint and verify tapped elements were preserved
        resume_state, _ = load_checkpoint(state.output_dir)
        screen_id = list(resume_state.screens.keys())[0]
        tapped = resume_state.screen_tapped_elements.get(screen_id, set())
        assert len(tapped) >= 1
