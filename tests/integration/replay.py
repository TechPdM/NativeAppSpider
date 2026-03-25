"""Replay mock classes for integration testing.

ReplayDevice and ReplayAnalyzer serve pre-defined sequences of responses,
letting us run the full Crawler pipeline without a real device or API key.
Each call pops the next response from a list, simulating a scripted crawl.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

from nativeappspider.analyzer import NavigationAction, ScreenAnalysis
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
        return self.get_clickable_elements()

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
