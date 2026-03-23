"""Claude Computer Use integration for screen analysis and navigation decisions."""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass

import anthropic
from PIL import Image


@dataclass
class ScreenAnalysis:
    """Analysis of a single screen."""

    screen_name: str
    description: str
    elements: list[dict]  # [{label, type, purpose, bounds}]
    suggested_actions: list[dict]  # [{action, target, reason, coordinates}]


@dataclass
class NavigationAction:
    """A single action the crawler should take."""

    action: str  # "tap", "swipe_up", "swipe_down", "back", "type"
    x: int = 0
    y: int = 0
    text: str = ""
    reason: str = ""


def _image_to_base64(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


class Analyzer:
    """Uses Claude to analyze screens and decide navigation."""

    def __init__(self, model: str = "claude-sonnet-4-6-20250514"):
        self._client = anthropic.Anthropic()
        self._model = model

    def analyze_screen(
        self,
        screenshot: Image.Image,
        ui_elements: list[dict] | None = None,
        visited_screens: list[str] | None = None,
        current_path: list[str] | None = None,
    ) -> ScreenAnalysis:
        """Analyze a screenshot and return structured screen documentation."""
        context_parts = []
        if ui_elements:
            context_parts.append(
                f"UI hierarchy elements (clickable): {json.dumps(ui_elements[:30], indent=2)}"
            )
        if visited_screens:
            context_parts.append(f"Already visited screens: {', '.join(visited_screens[-20:])}")
        if current_path:
            context_parts.append(f"Navigation path to here: {' → '.join(current_path)}")

        context = "\n\n".join(context_parts)

        response = self._client.messages.create(
            model=self._model,
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _image_to_base64(screenshot),
                        },
                    },
                    {
                        "type": "text",
                        "text": f"""Analyze this mobile app screen. Return a JSON object with:

{{
  "screen_name": "short descriptive name for this screen",
  "description": "what this screen is for and what the user can do here",
  "elements": [
    {{"label": "...", "type": "button|input|link|tab|menu|toggle|...", "purpose": "what it does"}}
  ],
  "suggested_actions": [
    {{"action": "tap|swipe_up|swipe_down|type", "target": "element description", "reason": "why explore this", "x": 0, "y": 0}}
  ]
}}

Focus on interactive elements. For suggested_actions, prioritize elements that likely lead to NEW screens we haven't visited yet. Include x,y coordinates for each action based on where elements appear in the screenshot.

{context}

Return ONLY valid JSON, no markdown fences.""",
                    },
                ],
            }],
        )

        text = response.content[0].text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {
                "screen_name": "parse_error",
                "description": text[:200],
                "elements": [],
                "suggested_actions": [],
            }

        return ScreenAnalysis(
            screen_name=data.get("screen_name", "unknown"),
            description=data.get("description", ""),
            elements=data.get("elements", []),
            suggested_actions=data.get("suggested_actions", []),
        )

    def decide_next_action(
        self,
        screenshot: Image.Image,
        clickable_elements: list[dict],
        visited_screens: list[str],
        exploration_goal: str = "Explore as many unique screens as possible",
    ) -> NavigationAction:
        """Decide which action to take next to maximize exploration coverage."""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _image_to_base64(screenshot),
                        },
                    },
                    {
                        "type": "text",
                        "text": f"""You are a mobile app crawler. Decide the single best next action to explore this app.

Goal: {exploration_goal}

Clickable elements on screen:
{json.dumps(clickable_elements[:20], indent=2)}

Already visited screens: {', '.join(visited_screens[-15:])}

Return JSON:
{{"action": "tap|swipe_up|swipe_down|back|type", "x": 0, "y": 0, "text": "", "reason": "why this action"}}

Pick actions that lead to UNVISITED screens. If this screen looks fully explored, use "back".
Return ONLY valid JSON.""",
                    },
                ],
            }],
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return NavigationAction(action="back", reason="failed to parse AI response")

        return NavigationAction(
            action=data.get("action", "back"),
            x=data.get("x", 0),
            y=data.get("y", 0),
            text=data.get("text", ""),
            reason=data.get("reason", ""),
        )
