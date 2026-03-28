"""Replay mock classes for integration testing.

ReplayDevice and ReplayAnalyzer serve pre-defined sequences of responses,
letting us run the full Crawler pipeline without a real device or API key.
Each call pops the next response from a list, simulating a scripted crawl.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from nativeappspider.analyzer import NavigationAction, ScreenAnalysis
from nativeappspider.crawler import CrawlConfig
from nativeappspider.device import UIElement


@dataclass
class DeviceStep:
    """What the device returns for one iteration of the crawl loop.

    Each crawl iteration calls screenshot(), get_clickable_elements(), and
    current_activity(). This bundles those three responses together so test
    scenarios read as a clear step-by-step script.
    """

    screenshot: Image.Image
    clickable: list[UIElement] = field(default_factory=list)
    activity: str = "com.test.app/.MainActivity"
    # Optional full UI hierarchy (including non-clickable and system elements).
    # When set, get_ui_hierarchy() returns this instead of clickable.
    ui_hierarchy: list[UIElement] | None = None


class ReplayDevice:
    """A fake Device that replays scripted responses.

    Serves DeviceSteps in order. Actions (tap, swipe, back, etc.) are
    recorded but have no effect — they just log what the crawler did.
    """

    def __init__(self, steps: list[DeviceStep], screen_size: tuple[int, int] = (1080, 1920)):
        self._steps = list(steps)
        self._step_index = 0
        self._screen_size = screen_size
        self.actions_performed: list[str] = []

    def _current_step(self) -> DeviceStep:
        """Return the current step, clamping to the last one if we've run out."""
        idx = min(self._step_index, len(self._steps) - 1)
        return self._steps[idx]

    def is_connected(self) -> bool:
        return True

    def get_screen_size(self) -> tuple[int, int]:
        return self._screen_size

    def screenshot(self) -> Image.Image:
        step = self._current_step()
        return step.screenshot

    def get_clickable_elements(self) -> list[UIElement]:
        step = self._current_step()
        return step.clickable

    def get_ui_hierarchy(self) -> list[UIElement]:
        step = self._current_step()
        if step.ui_hierarchy is not None:
            return step.ui_hierarchy
        return step.clickable

    def current_activity(self) -> str:
        step = self._current_step()
        return step.activity

    def is_package_installed(self, package: str) -> bool:
        return True

    def launch_app(self, package: str) -> None:
        self.actions_performed.append(f"launch:{package}")

    def tap(self, x: int, y: int) -> None:
        self.actions_performed.append(f"tap:{x},{y}")
        # Advance to next step after an action
        self._step_index += 1

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.actions_performed.append(f"swipe:{x1},{y1}->{x2},{y2}")
        self._step_index += 1

    def press_back(self) -> None:
        self.actions_performed.append("back")
        self._step_index += 1

    def press_home(self) -> None:
        self.actions_performed.append("home")

    def input_text(self, text: str) -> None:
        self.actions_performed.append(f"type:{text}")
        self._step_index += 1

    def force_stop(self, package: str) -> None:
        self.actions_performed.append(f"force_stop:{package}")

    def clear_app_data(self, package: str) -> None:
        self.actions_performed.append(f"clear:{package}")


@dataclass
class AnalyzerStep:
    """What the analyzer returns for one new screen + action decision.

    analysis is returned by analyze_screen() when a new screen is found.
    action is returned by decide_next_action() to navigate away from it.
    Set analyze_error to make analyze_screen() raise on this step.
    """

    analysis: ScreenAnalysis
    action: NavigationAction
    analyze_error: Exception | None = None


class ReplayAnalyzer:
    """A fake Analyzer that replays scripted analysis and navigation responses.

    New-screen analyses and navigation actions are served from a list.
    For revisited screens (where only decide_next_action is called),
    a default fallback action is used.
    """

    def __init__(self, steps: list[AnalyzerStep], fallback_action: NavigationAction | None = None):
        self._steps = list(steps)
        self._step_index = 0
        self._fallback_action = fallback_action or NavigationAction(
            action="back", reason="replay fallback"
        )

    def analyze_screen(self, screenshot, **kwargs) -> ScreenAnalysis:
        """Return the next scripted analysis, or raise if analyze_error is set."""
        if self._step_index < len(self._steps):
            step = self._steps[self._step_index]
            if step.analyze_error is not None:
                raise step.analyze_error
            return step.analysis
        # Ran out of scripted analyses — return a generic one
        return ScreenAnalysis(
            screen_name=f"extra_screen_{self._step_index}",
            description="Unscripted screen",
            elements=[],
            suggested_actions=[],
        )

    def decide_next_action(self, screenshot, clickable_elements, visited_screens, **kwargs) -> NavigationAction:
        """Return the next scripted action, then advance the step counter."""
        if self._step_index < len(self._steps):
            action = self._steps[self._step_index].action
            self._step_index += 1
            return action
        return self._fallback_action


def load_fixture(fixture_dir: Path) -> tuple[ReplayDevice, ReplayAnalyzer, CrawlConfig]:
    """Load a fixture directory and return replay mocks and config.

    Reads scenario.json and its screenshots to construct a ReplayDevice
    and ReplayAnalyzer that replay the recorded crawl sequence.
    """
    scenario = json.loads((fixture_dir / "scenario.json").read_text())

    device_steps: list[DeviceStep] = []
    analyzer_steps: list[AnalyzerStep] = []

    for step in scenario["steps"]:
        # Load screenshot
        ss_path = fixture_dir / step["screenshot_path"]
        screenshot = Image.open(ss_path)

        # Reconstruct UIElements
        clickable = [
            UIElement(
                resource_id=e.get("resource_id", ""),
                class_name=e.get("class_name", ""),
                text=e.get("text", ""),
                content_desc=e.get("content_desc", ""),
                package=e.get("package", ""),
                bounds=tuple(e.get("bounds", [0, 0, 0, 0])),
                clickable=e.get("clickable", True),
                scrollable=e.get("scrollable", False),
                enabled=e.get("enabled", True),
            )
            for e in step.get("clickable", [])
        ]

        device_steps.append(DeviceStep(
            screenshot=screenshot,
            clickable=clickable,
            activity=step.get("activity", "com.test.app/.MainActivity"),
        ))

        # Only build an AnalyzerStep if this was a new screen with analysis
        if step.get("is_new") and step.get("analysis"):
            a = step["analysis"]
            analysis = ScreenAnalysis(
                screen_name=a["screen_name"],
                description=a["description"],
                elements=a.get("elements", []),
                suggested_actions=a.get("suggested_actions", []),
                matches_focus_target=a.get("matches_focus_target", False),
            )

            act = step.get("action", {})
            action = NavigationAction(
                action=act.get("action", "back"),
                x=act.get("x", 0),
                y=act.get("y", 0),
                text=act.get("text", ""),
                reason=act.get("reason", ""),
            )

            analyzer_steps.append(AnalyzerStep(analysis=analysis, action=action))

    # Build config from recorded settings
    cfg = scenario.get("config", {})
    config = CrawlConfig(
        package=cfg.get("package", "com.test.app"),
        max_screens=cfg.get("max_screens", 50),
        max_actions=cfg.get("max_actions", 200),
        max_depth=cfg.get("max_depth", 10),
        hash_threshold=cfg.get("hash_threshold", 12),
        avoid_flows=cfg.get("avoid_flows", []),
        dismiss_flows=cfg.get("dismiss_flows", []),
        focus_screen=cfg.get("focus_screen"),
        scroll_discovery=cfg.get("scroll_discovery", True),
    )

    device = ReplayDevice(device_steps)
    analyzer = ReplayAnalyzer(analyzer_steps)
    return device, analyzer, config
