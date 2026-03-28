"""Claude vision API integration for screen analysis and navigation decisions."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import time
from dataclasses import dataclass

import anthropic
from PIL import Image

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds


@dataclass
class ScreenAnalysis:
    """Analysis of a single screen."""

    screen_name: str
    description: str
    elements: list[dict]  # [{label, type, purpose, bounds}]
    suggested_actions: list[dict]  # [{action, target, reason, coordinates}]
    matches_focus_target: bool = False


@dataclass
class NavigationAction:
    """A single action the crawler should take."""

    action: str  # "tap", "swipe_up", "swipe_down", "back", "type"
    x: int = 0
    y: int = 0
    text: str = ""
    reason: str = ""


def check_api_key() -> None:
    """Verify ANTHROPIC_API_KEY is set. Call before starting a crawl."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set.\n"
            "Set it with: export ANTHROPIC_API_KEY=your-key"
        )


def _image_to_base64(image: Image.Image) -> str:
    """Convert a PIL Image to a base64-encoded PNG string for the Claude API."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def _parse_json_response(text: str) -> dict:
    """Parse JSON from Claude's response, handling markdown fences."""
    text = text.strip()
    # Claude sometimes wraps JSON in markdown code fences — strip them if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _call_with_retry(client: anthropic.Anthropic, **kwargs) -> anthropic.types.Message:
    """Call the Claude API with exponential backoff on retryable errors."""
    for attempt in range(MAX_RETRIES):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            # Rate limits are expected during heavy crawls — back off and retry
            if attempt == MAX_RETRIES - 1:
                raise
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("Rate limited, retrying in %.1fs (attempt %d/%d)", delay, attempt + 1, MAX_RETRIES)
            time.sleep(delay)
        except anthropic.APIStatusError as e:
            # Retry on server errors (5xx) but not client errors (4xx)
            if e.status_code >= 500 and attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning("API error %d, retrying in %.1fs", e.status_code, delay)
                time.sleep(delay)
            else:
                raise
    raise RuntimeError("Unreachable")  # pragma: no cover


class Analyzer:
    """Uses Claude to analyze screens and decide navigation."""

    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(
        self,
        model: str | None = None,
        analysis_model: str | None = None,
        decision_model: str | None = None,
    ):
        check_api_key()
        self._client = anthropic.Anthropic()
        base = model or self.DEFAULT_MODEL
        self._analysis_model = analysis_model or base
        self._decision_model = decision_model or base

    def analyze_screen(
        self,
        screenshot: Image.Image,
        ui_elements: list[dict] | None = None,
        visited_screens: list[str] | None = None,
        current_path: list[str] | None = None,
        avoid_flows: list[str] | None = None,
        dismiss_flows: list[str] | None = None,
        focus_screen: str | None = None,
    ) -> ScreenAnalysis:
        """Analyze a screenshot and return structured screen documentation."""
        # Build context to help Claude make better decisions. Each piece of
        # context gives the model awareness of what's on screen, where we've
        # been, and what to avoid — all of which reduce redundant exploration.
        context_parts = []
        if ui_elements:
            # Cap at 30 elements to stay within token budget
            context_parts.append(
                f"UI hierarchy elements (clickable): {json.dumps(ui_elements[:30], indent=2)}"
            )
        if visited_screens:
            # Show the 20 most recent screens so Claude can suggest unvisited targets
            context_parts.append(f"Already visited screens: {', '.join(visited_screens[-20:])}")
        if current_path:
            context_parts.append(f"Navigation path to here: {' → '.join(current_path)}")
        if avoid_flows:
            context_parts.append(
                f"AVOIDED FLOWS: The following flows should be skipped: {', '.join(avoid_flows)}. "
                f"Note in the description if this screen is part of an avoided flow."
            )
        if dismiss_flows:
            context_parts.append(
                f"DISMISS SCREENS: If this screen relates to any of these: {', '.join(dismiss_flows)}, "
                f"note it in the description. These screens should be dismissed quickly, not explored."
            )
        if focus_screen:
            context_parts.append(
                f"TARGET SCREEN: Navigate toward the '{focus_screen}' screen. "
                f"Note in the description if this screen is on the path to the target."
            )

        context = "\n\n".join(context_parts)

        # When a focus target is set, ask Claude to judge whether this screen
        # IS the target — more reliable than fuzzy name matching
        focus_field = ""
        if focus_screen:
            focus_field = (
                f',\n  "matches_focus_target": true/false '
                f'// Is this the \'{focus_screen}\' screen itself (not just a screen that mentions it)?'
            )

        response = _call_with_retry(
            self._client,
            model=self._analysis_model,
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
  ]{focus_field}
}}

Focus on interactive elements. For suggested_actions, prioritize elements that likely lead to NEW screens we haven't visited yet. Include x,y coordinates for each action based on where elements appear in the screenshot.

{context}

Return ONLY valid JSON, no markdown fences.""",
                    },
                ],
            }],
        )

        return self._parse_screen_analysis(response.content[0].text)

    @staticmethod
    def _parse_screen_analysis(text: str) -> ScreenAnalysis:
        """Parse Claude's response into a ScreenAnalysis, with fallbacks."""
        try:
            data = _parse_json_response(text)
        except (json.JSONDecodeError, IndexError):
            logger.warning("Failed to parse screen analysis JSON, using fallback")
            return ScreenAnalysis(
                screen_name="parse_error",
                description=text[:200],
                elements=[],
                suggested_actions=[],
            )

        return ScreenAnalysis(
            screen_name=data.get("screen_name", "unknown") or "unknown",
            description=data.get("description", "") or "",
            elements=data.get("elements") if isinstance(data.get("elements"), list) else [],
            suggested_actions=data.get("suggested_actions") if isinstance(data.get("suggested_actions"), list) else [],
            matches_focus_target=bool(data.get("matches_focus_target", False)),
        )

    def decide_next_action(
        self,
        screenshot: Image.Image,
        clickable_elements: list[dict],
        visited_screens: list[str],
        recent_actions: list[str] | None = None,
        target_package: str | None = None,
        avoid_flows: list[str] | None = None,
        dismiss_flows: list[str] | None = None,
        focus_screen: str | None = None,
    ) -> NavigationAction:
        """Decide which action to take next to maximize exploration coverage."""
        context_parts = []
        if recent_actions:
            context_parts.append(
                f"Recent actions taken on this screen (DO NOT repeat these):\n"
                + "\n".join(f"  - {a}" for a in recent_actions[-5:])
            )
        if target_package:
            context_parts.append(
                f"Target app: {target_package}. Stay within this app. "
                f"If you've left the app (e.g. home screen), use 'back' to return."
            )
        if avoid_flows:
            context_parts.append(
                f"AVOID these flows — do NOT tap elements that lead into: {', '.join(avoid_flows)}. "
                f"If the current screen is part of an avoided flow, use \"back\" immediately."
            )
        if dismiss_flows:
            context_parts.append(
                f"DISMISS QUICKLY: If this screen relates to any of these: {', '.join(dismiss_flows)}, "
                f"tap the most obvious accept/ok/continue/dismiss button to get past it. "
                f"Do NOT explore these screens — just dismiss and move on."
            )
        if focus_screen:
            context_parts.append(
                f"TARGET SCREEN: Your primary goal is to reach the '{focus_screen}' screen. "
                f"Choose the action most likely to navigate toward it. "
                f"Dismiss dialogs, skip onboarding, and take the shortest path."
            )
        extra_context = "\n\n".join(context_parts)

        response = _call_with_retry(
            self._client,
            model=self._decision_model,
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

Clickable elements on screen:
{json.dumps(clickable_elements[:20], indent=2)}

Already visited screens: {', '.join(visited_screens[-15:])}

{extra_context}

Rules:
- Pick an action that leads to an UNVISITED screen
- Do NOT repeat an action that was already tried on this screen
- If all elements on this screen have been tried, use "back"
- Prefer navigation elements (tabs, menu items, links) over data entry or toggles

Return JSON:
{{"action": "tap|swipe_up|swipe_down|back|type", "x": 0, "y": 0, "text": "", "reason": "why this action"}}

Return ONLY valid JSON.""",
                    },
                ],
            }],
        )

        return self._parse_navigation_action(response.content[0].text)

    @staticmethod
    def _parse_navigation_action(text: str) -> NavigationAction:
        """Parse Claude's response into a NavigationAction, with fallbacks."""
        try:
            data = _parse_json_response(text)
        except (json.JSONDecodeError, IndexError):
            logger.warning("Failed to parse navigation action JSON, falling back to 'back'")
            return NavigationAction(action="back", reason="failed to parse AI response")

        # Validate the action type — fall back to "back" for anything unexpected
        # so the crawler always makes progress instead of crashing
        action = data.get("action", "back")
        if action not in ("tap", "swipe_up", "swipe_down", "back", "type"):
            logger.warning("Unknown action '%s', falling back to 'back'", action)
            action = "back"

        return NavigationAction(
            action=action,
            x=int(data.get("x", 0) or 0),
            y=int(data.get("y", 0) or 0),
            text=str(data.get("text", "") or ""),
            reason=str(data.get("reason", "") or ""),
        )
