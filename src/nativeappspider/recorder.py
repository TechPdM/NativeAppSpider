"""Live crawl recorder for capturing fixture data.

Records each step of a crawl as it happens — screenshots, clickable
elements, analysis results, and navigation actions. Writes a
recording.json to the crawl output directory that can be converted
into replay test fixtures.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

from nativeappspider.analyzer import NavigationAction, ScreenAnalysis
from nativeappspider.device import UIElement


@dataclass
class RecordedStep:
    """One iteration of the crawl loop, fully captured."""

    iteration: int
    screenshot_path: str
    activity: str
    is_new: bool
    screen_id: str
    clickable: list[dict] = field(default_factory=list)
    analysis: dict | None = None
    action: dict | None = None


class CrawlRecorder:
    """Records crawl steps for later replay as test fixtures."""

    def __init__(self, output_dir: Path, config: dict):
        self._output_dir = output_dir
        self._config = config
        self._steps: list[RecordedStep] = []
        self._current_step: RecordedStep | None = None

    def begin_step(
        self,
        iteration: int,
        screenshot: Image.Image,
        screenshot_path: str,
        screen_id: str,
        is_new: bool,
        activity: str,
        clickable: list[UIElement],
    ) -> None:
        """Start recording a new crawl iteration."""
        self._current_step = RecordedStep(
            iteration=iteration,
            screenshot_path=screenshot_path,
            activity=activity,
            is_new=is_new,
            screen_id=screen_id,
            clickable=[_serialize_element(e) for e in clickable],
        )

    def record_analysis(self, analysis: ScreenAnalysis) -> None:
        """Record the screen analysis result for the current step."""
        if self._current_step is None:
            return
        self._current_step.analysis = {
            "screen_name": analysis.screen_name,
            "description": analysis.description,
            "elements": analysis.elements,
            "suggested_actions": analysis.suggested_actions,
            "matches_focus_target": analysis.matches_focus_target,
        }

    def record_action(self, action: NavigationAction) -> None:
        """Record the navigation action for the current step."""
        if self._current_step is None:
            return
        self._current_step.action = {
            "action": action.action,
            "x": action.x,
            "y": action.y,
            "text": action.text,
            "reason": action.reason,
        }

    def end_step(self) -> None:
        """Finalize and store the current step."""
        if self._current_step is not None:
            self._steps.append(self._current_step)
            self._current_step = None

    def save(self) -> None:
        """Write recording.json to the output directory."""
        data = {
            "config": self._config,
            "steps": [asdict(s) for s in self._steps],
        }
        path = self._output_dir / "recording.json"
        path.write_text(json.dumps(data, indent=2))


def _serialize_element(e: UIElement) -> dict:
    """Convert a UIElement to a JSON-serializable dict."""
    return {
        "resource_id": e.resource_id,
        "class_name": e.class_name,
        "text": e.text,
        "content_desc": e.content_desc,
        "package": e.package,
        "bounds": list(e.bounds),
        "clickable": e.clickable,
        "scrollable": e.scrollable,
        "enabled": e.enabled,
    }
