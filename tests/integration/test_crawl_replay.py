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

import numpy as np
from PIL import Image

from nativeappspider.analyzer import NavigationAction, ScreenAnalysis
from nativeappspider.crawler import CrawlConfig, Crawler
from nativeappspider.device import UIElement

from .replay import AnalyzerStep, DeviceStep, ReplayAnalyzer, ReplayDevice


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


def _make_element(label: str, bounds: tuple[int, int, int, int] = (0, 0, 200, 80)) -> UIElement:
    return UIElement(
        resource_id=f"com.test.app:id/{label.lower().replace(' ', '_')}",
        class_name="android.widget.Button",
        text=label,
        content_desc="",
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
        crawler = Crawler(config, device)
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
    """When Claude gives the same name to two different screens, the
    output should disambiguate them with numeric suffixes.
    """

    def test_deduplicates_screen_names_in_output(self, tmp_path):
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
        crawler = _make_crawler(tmp_path, device, analyzer, max_actions=3)

        state = crawler.crawl()

        # After dedup, names should be unique (e.g. "Settings (1)", "Settings (2)")
        names = [s.screen_name for s in state.screens.values()]
        assert len(set(names)) == 2  # All names are unique
        assert all("Settings" in n for n in names)  # Both still contain "Settings"

        # Verify the output JSON also has deduplicated names
        screens_json = json.loads((state.output_dir / "screens.json").read_text())
        json_names = [s["screen_name"] for s in screens_json.values()]
        assert len(set(json_names)) == 2


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

        # Same screen every step — the crawler stays on it
        device_steps = [
            DeviceStep(screenshot=img, clickable=[_make_element("Btn")]),
        ]
        analyzer_steps = [
            AnalyzerStep(
                analysis=ScreenAnalysis("Home", "Home screen", [], []),
                action=NavigationAction(action="tap", x=100, y=40, reason="try button"),
            ),
        ]

        device = ReplayDevice(device_steps)
        analyzer = ReplayAnalyzer(
            analyzer_steps,
            fallback_action=NavigationAction(action="tap", x=200, y=80, reason="try another"),
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
