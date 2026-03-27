"""Main crawl loop and state graph management."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import networkx as nx
from PIL import Image

from nativeappspider.analyzer import Analyzer, NavigationAction, ScreenAnalysis
from nativeappspider.device import ADBError, Device
from nativeappspider.hasher import are_similar, screen_hash

logger = logging.getLogger(__name__)

# Packages that indicate a system dialog overlay rather than app content.
# When elements from these packages appear, auto-dismiss instead of analyzing.
SYSTEM_DIALOG_PACKAGES = frozenset({
    "com.android.packageinstaller",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "android",
    "com.android.systemui",
})

# Button labels commonly found on system dialogs — matched case-insensitively.
DIALOG_DISMISS_LABELS = frozenset({
    "allow", "ok", "deny", "dismiss", "cancel", "close",
    "got it", "continue", "not now", "skip", "while using the app",
    "only this time", "don't allow", "don\u2019t allow",
})


@dataclass
class ScreenNode:
    """A unique screen in the app's state graph."""

    screen_id: str  # perceptual hash
    screen_name: str
    description: str
    activity: str
    elements: list[dict]
    screenshot_path: str
    visit_count: int = 0
    first_seen: str = ""


@dataclass
class CrawlConfig:
    """Configuration for a crawl session."""

    package: str
    max_screens: int = 50
    max_depth: int = 10
    max_actions: int = 200
    settle_delay: float = 1.5  # seconds to wait after each action
    output_dir: str = "output"
    hash_threshold: int = 12
    avoid_flows: list[str] = field(default_factory=list)
    focus_screen: str | None = None
    scroll_discovery: bool = True


@dataclass
class CrawlState:
    """Tracks the current state of a crawl session."""

    graph: nx.DiGraph = field(default_factory=nx.DiGraph)
    screens: dict[str, ScreenNode] = field(default_factory=dict)
    action_count: int = 0
    current_path: list[str] = field(default_factory=list)
    output_dir: Path = field(default_factory=lambda: Path("output"))
    # Track actions tried per screen to avoid repeating them
    screen_actions: dict[str, list[str]] = field(default_factory=dict)
    focus_reached: bool = False
    # Track which specific elements have been tapped per screen, using
    # (bounds, label) tuples as structural identifiers. This enables precise
    # "all elements explored" detection without relying on Claude.
    screen_tapped_elements: dict[str, set[tuple]] = field(default_factory=dict)

    def find_matching_screen(self, hash_val: str, threshold: int = 12) -> str | None:
        """Find an existing screen that matches the given hash."""
        for sid in self.screens:
            if are_similar(sid, hash_val, threshold):
                return sid
        return None


class Crawler:
    """Orchestrates the app crawling process."""

    def __init__(self, config: CrawlConfig, device: Device | None = None, model: str | None = None):
        self.config = config
        self.device = device or Device()
        self.analyzer = Analyzer(model=model) if model else Analyzer()
        self.state = CrawlState()

    def crawl(self) -> CrawlState:
        """Run the main crawl loop."""
        self._screen_w, self._screen_h = self.device.get_screen_size()
        logger.info("Device screen: %dx%d", self._screen_w, self._screen_h)

        # Set up output directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.state.output_dir = Path(self.config.output_dir) / f"{self.config.package}_{timestamp}"
        self.state.output_dir.mkdir(parents=True, exist_ok=True)
        (self.state.output_dir / "screenshots").mkdir(exist_ok=True)

        # Launch the app
        print(f"Launching {self.config.package}...")
        self.device.launch_app(self.config.package)
        time.sleep(self.config.settle_delay * 2)  # Extra wait for app launch

        # --- Main crawl loop bookkeeping ---
        consecutive_known = 0          # how many steps in a row hit an already-seen screen
        consecutive_failures = 0       # how many screenshots in a row failed (device issues)
        max_consecutive_failures = 10  # bail out if device seems unreachable
        consecutive_relaunches = 0     # how many times we've re-launched the app back-to-back
        max_relaunches = 3             # after this many, try pressing back instead
        prev_screen: str | None = None       # screen we were on before the last action
        prev_action: NavigationAction | None = None  # last action taken (for recording transitions)

        # Keep exploring until we hit either the action budget or the screen
        # discovery limit — whichever comes first
        while (
            self.state.action_count < self.config.max_actions
            and len(self.state.screens) < self.config.max_screens
        ):
            try:
                screenshot, screen_id, is_new = self._capture_and_identify()
                consecutive_failures = 0
            except ADBError as e:
                consecutive_failures += 1
                logger.error("Screenshot failed (%d/%d): %s",
                             consecutive_failures, max_consecutive_failures, e)
                if consecutive_failures >= max_consecutive_failures:
                    logger.error("Too many consecutive screenshot failures, stopping crawl")
                    break
                time.sleep(self.config.settle_delay)
                continue

            # Check for system dialog overlays (permission prompts, ANR, etc.)
            # before processing the screen — dismiss and re-loop if found
            if self._detect_and_dismiss_dialog():
                self.state.action_count += 1
                continue

            # Fetch clickable elements once per iteration — reused by both the
            # screen analyzer (to document elements) and the action decider
            clickable = self.device.get_clickable_elements()

            if is_new:
                consecutive_known = 0
                try:
                    # Send the screenshot to Claude for analysis and documentation
                    self._process_new_screen(screenshot, screen_id, clickable)
                except Exception as e:
                    # If Claude fails (network, parsing, etc.), still record the
                    # screen so we don't keep trying to analyze it on revisits
                    logger.error("Failed to analyze screen: %s", e)
                    self._record_minimal_screen(screenshot, screen_id)

                # Scroll through any scrollable containers to discover
                # off-screen elements that aren't visible in the initial viewport
                if self.config.scroll_discovery:
                    clickable = self._discover_scrollable_elements(screen_id, clickable)
            else:
                self.state.screens[screen_id].visit_count += 1
                consecutive_known += 1
                print(f"  [revisit] {self.state.screens[screen_id].screen_name} "
                      f"(visited {self.state.screens[screen_id].visit_count}x)")

            # Record the state transition as an edge in the graph. We only
            # record it when the screen actually changed (i.e. the action navigated
            # somewhere new, not just refreshed the same screen).
            if prev_screen is not None and prev_action is not None and screen_id != prev_screen:
                self.state.graph.add_edge(
                    prev_screen, screen_id,
                    action=prev_action.action,
                    reason=prev_action.reason,
                )

            # Check if we've left the target app (e.g. tapped a deep link or ad
            # that opened a browser). If so, try to get back into the target app.
            if self._is_outside_target_app():
                consecutive_relaunches += 1
                if consecutive_relaunches > max_relaunches:
                    # Re-launching keeps landing outside the app — try pressing
                    # back instead, which sometimes returns us to the target app
                    logger.error("Too many consecutive relaunches (%d), pressing back instead",
                                 consecutive_relaunches)
                    try:
                        self.device.press_back()
                        time.sleep(self.config.settle_delay)
                    except ADBError:
                        pass
                    self.state.action_count += 1
                    # After several back attempts with no luck, reset the counter
                    # so we'll try re-launching again on the next iteration
                    if consecutive_relaunches > max_relaunches + 3:
                        consecutive_relaunches = 0
                    continue

                print(f"  [relaunch] Left target app, re-launching {self.config.package}")
                try:
                    self.device.launch_app(self.config.package)
                    time.sleep(self.config.settle_delay * 2)
                    self.state.current_path.clear()
                    consecutive_known = 0
                except ADBError as e:
                    logger.error("Re-launch failed: %s", e)
                self.state.action_count += 1
                prev_screen = None
                prev_action = None
                continue
            else:
                consecutive_relaunches = 0

            # Track our navigation depth. Only append if the screen isn't
            # already in the path (avoids duplicates from revisits).
            if screen_id not in self.state.current_path:
                self.state.current_path.append(screen_id)

            # Decide what to do next
            action = self._decide_action(
                screenshot, screen_id, consecutive_known, clickable,
            )

            # Execute the action
            prev_screen = screen_id
            prev_action = action
            try:
                self._execute_action(action)
            except ADBError as e:
                logger.error("Action failed: %s", e)

            self.state.action_count += 1
            print(f"  [{self.state.action_count}] {action.action} → {action.reason}")

            # Record which element was tapped so the per-element loop
            # detector knows this element has been tried on this screen
            if action.action == "tap":
                self._record_tapped_element(screen_id, action, clickable)

            time.sleep(self.config.settle_delay)

            # Pressing back pops the navigation stack so our depth tracking
            # stays in sync with the device's actual back stack
            if action.action == "back" and self.state.current_path:
                self.state.current_path.pop()

        # Save crawl results
        self._save_results()
        print(f"\nCrawl complete: {len(self.state.screens)} screens, "
              f"{self.state.action_count} actions")
        return self.state

    def _detect_and_dismiss_dialog(self) -> bool:
        """Check for system dialog overlays and auto-dismiss them.

        System dialogs (permission prompts, ANR, etc.) sit on top of the
        target activity without changing it. We detect them by checking
        element package names against known system packages, then tap a
        dismiss button or press back.

        Returns True if a dialog was detected and dismissed.
        """
        try:
            hierarchy = self.device.get_ui_hierarchy()
        except ADBError:
            return False

        # Check if any element belongs to a system dialog package
        dialog_elements = [e for e in hierarchy if e.package in SYSTEM_DIALOG_PACKAGES]
        if not dialog_elements:
            return False

        print("  [dialog] System dialog detected, auto-dismissing...")

        # Look for a clickable dismiss button
        for e in dialog_elements:
            if not e.clickable or not e.enabled:
                continue
            label = (e.text or e.content_desc).lower()
            if label in DIALOG_DISMISS_LABELS:
                cx, cy = e.center
                self.device.tap(cx, cy)
                time.sleep(self.config.settle_delay)
                return True

        # No recognizable button found — press back to dismiss
        self.device.press_back()
        time.sleep(self.config.settle_delay)
        return True

    def _discover_scrollable_elements(
        self, screen_id: str, initial_clickable: list,
    ) -> list:
        """Scroll through scrollable containers to reveal off-screen elements.

        After a new screen is processed, checks for scrollable containers in the
        UI hierarchy. For each one, scrolls within its bounds and re-fetches
        the element list to discover elements that were below the fold.

        Returns the combined list of all discovered clickable elements.
        """
        MAX_SCROLLS_PER_CONTAINER = 5

        hierarchy = self.device.get_ui_hierarchy()
        scrollable = [e for e in hierarchy if e.scrollable]
        if not scrollable:
            return initial_clickable

        # Track all known elements by structural key
        known_keys = {(e.bounds, e.label) for e in initial_clickable}
        all_elements = list(initial_clickable)

        for container in scrollable:
            x1, y1, x2, y2 = container.bounds
            cx = (x1 + x2) // 2
            # Swipe within the container: from 75% to 25% of its height
            swipe_from_y = y1 + (y2 - y1) * 3 // 4
            swipe_to_y = y1 + (y2 - y1) // 4

            for _ in range(MAX_SCROLLS_PER_CONTAINER):
                self.device.swipe(cx, swipe_from_y, cx, swipe_to_y)
                time.sleep(self.config.settle_delay)

                new_clickable = self.device.get_clickable_elements()
                new_keys = {(e.bounds, e.label) for e in new_clickable}
                newly_found = new_keys - known_keys

                if not newly_found:
                    break  # No new elements revealed — stop scrolling

                print(f"  [scroll] Found {len(newly_found)} new elements")
                for e in new_clickable:
                    if (e.bounds, e.label) not in known_keys:
                        all_elements.append(e)
                        known_keys.add((e.bounds, e.label))

        return all_elements

    def _is_outside_target_app(self) -> bool:
        """Check if the foreground activity belongs to a different app."""
        activity = self.device.current_activity()
        if activity == "unknown":
            return False  # Can't tell, assume we're still in the app
        return self.config.package not in activity

    def _visited_screen_names(self) -> list[str]:
        return [s.screen_name for s in self.state.screens.values()]

    def _capture_and_identify(self) -> tuple[Image.Image, str, bool]:
        """Capture screenshot, hash it, return (image, screen_id, is_new)."""
        screenshot = self.device.screenshot()
        current_hash = screen_hash(screenshot)

        existing = self.state.find_matching_screen(current_hash, self.config.hash_threshold)
        if existing:
            return screenshot, existing, False
        return screenshot, current_hash, True

    def _process_new_screen(
        self, screenshot: Image.Image, screen_id: str, clickable: list,
    ) -> None:
        """Analyze and record a new screen."""
        ui_elements = [
            {"label": e.label, "bounds": e.bounds, "class": e.class_name}
            for e in clickable
        ]

        # Only pass focus_screen to the analyzer during the navigation phase
        active_focus = (
            self.config.focus_screen if not self.state.focus_reached else None
        )

        print(f"  [NEW] Analyzing screen ({len(self.state.screens) + 1})...")
        analysis = self.analyzer.analyze_screen(
            screenshot,
            ui_elements=ui_elements,
            visited_screens=self._visited_screen_names(),
            current_path=self.state.current_path,
            avoid_flows=self.config.avoid_flows or None,
            focus_screen=active_focus,
        )

        # Check if this screen matches the focus target
        if (
            self.config.focus_screen
            and not self.state.focus_reached
            and analysis.matches_focus_target
        ):
            self.state.focus_reached = True
            print(f"  [focus] Reached target screen: {analysis.screen_name}")

        self._record_screen(screenshot, screen_id, analysis)
        print(f"         → {analysis.screen_name}: {analysis.description[:80]}")

    def _record_screen(
        self, screenshot: Image.Image, screen_id: str, analysis: ScreenAnalysis,
    ) -> None:
        """Save a screen to the state graph and disk."""
        ss_path = self.state.output_dir / "screenshots" / f"{screen_id[:16]}.png"
        screenshot.save(ss_path)

        node = ScreenNode(
            screen_id=screen_id,
            screen_name=analysis.screen_name,
            description=analysis.description,
            activity=self.device.current_activity(),
            elements=analysis.elements,
            screenshot_path=str(ss_path),
            visit_count=1,
            first_seen=datetime.now().isoformat(),
        )
        self.state.screens[screen_id] = node
        self.state.graph.add_node(screen_id, name=analysis.screen_name)

    def _record_minimal_screen(self, screenshot: Image.Image, screen_id: str) -> None:
        """Record a screen with minimal info when analysis fails."""
        analysis = ScreenAnalysis(
            screen_name=f"screen_{len(self.state.screens) + 1}",
            description="Analysis failed",
            elements=[],
            suggested_actions=[],
        )
        self._record_screen(screenshot, screen_id, analysis)

    def _decide_action(
        self,
        screenshot: Image.Image,
        screen_id: str,
        consecutive_known: int,
        clickable: list,
    ) -> NavigationAction:
        """Decide the next navigation action.

        Uses heuristic short-circuits first (stuck detection, depth limit),
        then falls back to Claude for intelligent exploration decisions.
        """
        # Safety net: if we keep landing on already-seen screens, back out.
        # Threshold is 10 (not 5) because per-element tracking below handles
        # the common case more precisely.
        if consecutive_known > 10:
            return NavigationAction(action="back", reason="stuck in loop")
        # Don't go deeper than the configured limit — breadth-first is more useful
        if len(self.state.current_path) > self.config.max_depth:
            return NavigationAction(action="back", reason="max depth reached")

        # Check if every clickable element on this screen has already been
        # tapped. If so, there's nothing new to try — back out immediately
        # without spending an API call on Claude.
        tapped = self.state.screen_tapped_elements.get(screen_id, set())
        clickable_keys = {(e.bounds, e.label) for e in clickable}
        if clickable_keys and not (clickable_keys - tapped):
            return NavigationAction(action="back", reason="all elements explored")

        try:
            elements_for_ai = [
                {"label": e.label, "center": e.center, "class": e.class_name}
                for e in clickable
            ]
            recent_actions = self.state.screen_actions.get(screen_id, [])
            active_focus = (
                self.config.focus_screen if not self.state.focus_reached else None
            )
            action = self.analyzer.decide_next_action(
                screenshot,
                elements_for_ai,
                self._visited_screen_names(),
                recent_actions=recent_actions,
                target_package=self.config.package,
                avoid_flows=self.config.avoid_flows or None,
                focus_screen=active_focus,
            )
            # Record what we did on this screen so Claude won't suggest the
            # same action again on future visits to this screen
            action_desc = f"{action.action} at ({action.x},{action.y}) {action.reason[:60]}"
            self.state.screen_actions.setdefault(screen_id, []).append(action_desc)
            return action
        except Exception as e:
            logger.error("Navigation decision failed: %s", e)
            return NavigationAction(action="back", reason=f"decision error: {e}")

    def _record_tapped_element(
        self, screen_id: str, action: NavigationAction, clickable: list,
    ) -> None:
        """Record which element was tapped using the closest clickable element's
        structural identity (bounds + label). This feeds the per-element loop
        detector in _decide_action().
        """
        best = None
        best_dist = float("inf")
        for e in clickable:
            cx, cy = e.center
            dist = abs(cx - action.x) + abs(cy - action.y)
            if dist < best_dist:
                best_dist = dist
                best = e
        if best is not None:
            self.state.screen_tapped_elements.setdefault(screen_id, set()).add(
                (best.bounds, best.label)
            )

    def _execute_action(self, action: NavigationAction) -> None:
        """Execute a navigation action on the device.

        Swipes use the center of the screen and cover half the screen height
        to reliably scroll content without triggering edge gestures.
        """
        w, h = self._screen_w, self._screen_h
        match action.action:
            case "tap":
                self.device.tap(action.x, action.y)
            case "swipe_up":
                # Swipe from 75% to 25% of screen height (scrolls content up)
                self.device.swipe(w // 2, h * 3 // 4, w // 2, h // 4)
            case "swipe_down":
                # Swipe from 25% to 75% of screen height (scrolls content down)
                self.device.swipe(w // 2, h // 4, w // 2, h * 3 // 4)
            case "back":
                self.device.press_back()
            case "type":
                self.device.input_text(action.text)
            case _:
                # Unknown action type — safest fallback is pressing back
                self.device.press_back()

    def _deduplicate_screen_names(self) -> None:
        """Append numeric suffixes to duplicate screen names.

        Claude sometimes gives the same name to visually similar but distinct
        screens (e.g. two different "Settings" screens). This ensures unique
        names in the final report and Mermaid diagram.
        """
        name_counts: dict[str, int] = {}
        for node in self.state.screens.values():
            name_counts[node.screen_name] = name_counts.get(node.screen_name, 0) + 1

        # Only rename if there are actual duplicates
        dupes = {name for name, count in name_counts.items() if count > 1}
        if not dupes:
            return

        dupe_index: dict[str, int] = {}
        for node in self.state.screens.values():
            if node.screen_name in dupes:
                idx = dupe_index.get(node.screen_name, 1)
                dupe_index[node.screen_name] = idx + 1
                node.screen_name = f"{node.screen_name} ({idx})"

    def _save_results(self) -> None:
        """Save crawl state to JSON and Mermaid diagram."""
        self._deduplicate_screen_names()
        out = self.state.output_dir

        # Save screen data
        screens_data = {}
        for sid, node in self.state.screens.items():
            screens_data[sid] = {
                "screen_name": node.screen_name,
                "description": node.description,
                "activity": node.activity,
                "elements": node.elements,
                "screenshot": node.screenshot_path,
                "visit_count": node.visit_count,
                "first_seen": node.first_seen,
            }
        (out / "screens.json").write_text(json.dumps(screens_data, indent=2))

        # Save graph edges
        edges = []
        for u, v, data in self.state.graph.edges(data=True):
            u_name = self.state.screens[u].screen_name if u in self.state.screens else u[:8]
            v_name = self.state.screens[v].screen_name if v in self.state.screens else v[:8]
            edges.append({
                "from": u_name,
                "to": v_name,
                "action": data.get("action", ""),
                "reason": data.get("reason", ""),
            })
        (out / "transitions.json").write_text(json.dumps(edges, indent=2))

        # Generate Mermaid diagram — a top-down flowchart where each screen
        # is a node and each navigation action is a labeled edge
        mermaid = ["graph TD"]
        node_ids: dict[str, str] = {}  # map screen hash → short Mermaid node ID
        for i, (sid, node) in enumerate(self.state.screens.items()):
            nid = f"S{i}"
            node_ids[sid] = nid
            safe_name = node.screen_name.replace('"', "'")  # quotes break Mermaid syntax
            mermaid.append(f'    {nid}["{safe_name}"]')

        for u, v, data in self.state.graph.edges(data=True):
            if u in node_ids and v in node_ids:
                action = data.get("action", "")
                mermaid.append(f"    {node_ids[u]} -->|{action}| {node_ids[v]}")

        (out / "flow.mmd").write_text("\n".join(mermaid) + "\n")

        print(f"Results saved to {out}/")
