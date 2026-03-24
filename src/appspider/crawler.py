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

from appspider.analyzer import Analyzer, NavigationAction, ScreenAnalysis
from appspider.device import ADBError, Device
from appspider.hasher import are_similar, screen_hash

logger = logging.getLogger(__name__)


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
        # Validate prerequisites
        if not self.device.is_connected():
            raise RuntimeError("No Android device connected. Start an emulator or connect a device.")

        screen_w, screen_h = self.device.get_screen_size()
        logger.info("Device screen: %dx%d", screen_w, screen_h)

        # Set up output directory
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.state.output_dir = Path(self.config.output_dir) / f"{self.config.package}_{timestamp}"
        self.state.output_dir.mkdir(parents=True, exist_ok=True)
        (self.state.output_dir / "screenshots").mkdir(exist_ok=True)

        # Launch the app
        print(f"Launching {self.config.package}...")
        self.device.launch_app(self.config.package)
        time.sleep(self.config.settle_delay * 2)  # Extra wait for app launch

        # Main crawl loop
        consecutive_known = 0
        consecutive_failures = 0
        max_consecutive_failures = 10
        prev_screen: str | None = None
        prev_action: NavigationAction | None = None

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

            if is_new:
                consecutive_known = 0
                try:
                    self._process_new_screen(screenshot, screen_id)
                except Exception as e:
                    logger.error("Failed to analyze screen: %s", e)
                    self._record_minimal_screen(screenshot, screen_id)
            else:
                self.state.screens[screen_id].visit_count += 1
                consecutive_known += 1
                print(f"  [revisit] {self.state.screens[screen_id].screen_name} "
                      f"(visited {self.state.screens[screen_id].visit_count}x)")

            # Record transition from previous action (now that current screen is identified)
            if prev_screen is not None and prev_action is not None and screen_id != prev_screen:
                self.state.graph.add_edge(
                    prev_screen, screen_id,
                    action=prev_action.action,
                    reason=prev_action.reason,
                )

            # Check if we've left the target app — re-launch if so
            if self._is_outside_target_app():
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

            # Update path
            if screen_id not in self.state.current_path:
                self.state.current_path.append(screen_id)

            # Decide what to do next
            action = self._decide_action(
                screenshot, screen_id, consecutive_known, screen_w, screen_h,
            )

            # Execute the action
            prev_screen = screen_id
            prev_action = action
            try:
                self._execute_action(action, screen_w, screen_h)
            except ADBError as e:
                logger.error("Action failed: %s", e)

            self.state.action_count += 1
            print(f"  [{self.state.action_count}] {action.action} → {action.reason}")

            time.sleep(self.config.settle_delay)

            if action.action == "back" and self.state.current_path:
                self.state.current_path.pop()

        # Save crawl results
        self._save_results()
        print(f"\nCrawl complete: {len(self.state.screens)} screens, "
              f"{self.state.action_count} actions")
        return self.state

    def _is_outside_target_app(self) -> bool:
        """Check if the foreground activity belongs to a different app."""
        activity = self.device.current_activity()
        if activity == "unknown":
            return False  # Can't tell, assume we're still in the app
        return self.config.package not in activity

    def _capture_and_identify(self) -> tuple[Image.Image, str, bool]:
        """Capture screenshot, hash it, return (image, screen_id, is_new)."""
        screenshot = self.device.screenshot()
        current_hash = screen_hash(screenshot)

        existing = self.state.find_matching_screen(current_hash, self.config.hash_threshold)
        if existing:
            return screenshot, existing, False
        return screenshot, current_hash, True

    def _process_new_screen(self, screenshot: Image.Image, screen_id: str) -> None:
        """Analyze and record a new screen."""
        clickable = self.device.get_clickable_elements()
        ui_elements = [
            {"label": e.label, "bounds": e.bounds, "class": e.class_name}
            for e in clickable
        ]

        print(f"  [NEW] Analyzing screen ({len(self.state.screens) + 1})...")
        analysis = self.analyzer.analyze_screen(
            screenshot,
            ui_elements=ui_elements,
            visited_screens=[s.screen_name for s in self.state.screens.values()],
            current_path=self.state.current_path,
        )

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
        screen_w: int,
        screen_h: int,
    ) -> NavigationAction:
        """Decide the next navigation action."""
        if consecutive_known > 5:
            return NavigationAction(action="back", reason="stuck in loop")
        if len(self.state.current_path) > self.config.max_depth:
            return NavigationAction(action="back", reason="max depth reached")

        try:
            clickable = self.device.get_clickable_elements()
            elements_for_ai = [
                {"label": e.label, "center": e.center, "class": e.class_name}
                for e in clickable
            ]
            recent_actions = self.state.screen_actions.get(screen_id, [])
            action = self.analyzer.decide_next_action(
                screenshot,
                elements_for_ai,
                [s.screen_name for s in self.state.screens.values()],
                recent_actions=recent_actions,
                target_package=self.config.package,
            )
            # Record this action so we don't repeat it on this screen
            action_desc = f"{action.action} at ({action.x},{action.y}) {action.reason[:60]}"
            self.state.screen_actions.setdefault(screen_id, []).append(action_desc)
            return action
        except Exception as e:
            logger.error("Navigation decision failed: %s", e)
            return NavigationAction(action="back", reason=f"decision error: {e}")

    def _execute_action(self, action: NavigationAction, screen_w: int, screen_h: int) -> None:
        """Execute a navigation action on the device."""
        match action.action:
            case "tap":
                self.device.tap(action.x, action.y)
            case "swipe_up":
                self.device.swipe(screen_w // 2, screen_h * 3 // 4, screen_w // 2, screen_h // 4)
            case "swipe_down":
                self.device.swipe(screen_w // 2, screen_h // 4, screen_w // 2, screen_h * 3 // 4)
            case "back":
                self.device.press_back()
            case "type":
                self.device.input_text(action.text)
            case _:
                self.device.press_back()

    def _deduplicate_screen_names(self) -> None:
        """Append numeric suffixes to duplicate screen names."""
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
            u_name = self.state.screens.get(u, ScreenNode(u, u[:8], "", "", [], "")).screen_name
            v_name = self.state.screens.get(v, ScreenNode(v, v[:8], "", "", [], "")).screen_name
            edges.append({
                "from": u_name,
                "to": v_name,
                "action": data.get("action", ""),
                "reason": data.get("reason", ""),
            })
        (out / "transitions.json").write_text(json.dumps(edges, indent=2))

        # Generate Mermaid diagram
        mermaid = ["graph TD"]
        node_ids: dict[str, str] = {}
        for i, (sid, node) in enumerate(self.state.screens.items()):
            nid = f"S{i}"
            node_ids[sid] = nid
            safe_name = node.screen_name.replace('"', "'")
            mermaid.append(f'    {nid}["{safe_name}"]')

        for u, v, data in self.state.graph.edges(data=True):
            if u in node_ids and v in node_ids:
                action = data.get("action", "")
                mermaid.append(f"    {node_ids[u]} -->|{action}| {node_ids[v]}")

        (out / "flow.mmd").write_text("\n".join(mermaid) + "\n")

        print(f"Results saved to {out}/")
