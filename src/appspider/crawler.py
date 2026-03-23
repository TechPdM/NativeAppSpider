"""Main crawl loop and state graph management."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import networkx as nx
from PIL import Image

from appspider.analyzer import Analyzer, NavigationAction, ScreenAnalysis
from appspider.device import Device
from appspider.hasher import are_similar, screen_hash


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

    def find_matching_screen(self, hash_val: str, threshold: int = 12) -> str | None:
        """Find an existing screen that matches the given hash."""
        for sid in self.screens:
            if are_similar(sid, hash_val, threshold):
                return sid
        return None


class Crawler:
    """Orchestrates the app crawling process."""

    def __init__(self, config: CrawlConfig, device: Device | None = None):
        self.config = config
        self.device = device or Device()
        self.analyzer = Analyzer()
        self.state = CrawlState()

    def crawl(self) -> CrawlState:
        """Run the main crawl loop."""
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
        while (
            self.state.action_count < self.config.max_actions
            and len(self.state.screens) < self.config.max_screens
        ):
            # Capture current state
            screenshot = self.device.screenshot()
            current_hash = screen_hash(screenshot)

            # Check if we've seen this screen
            existing = self.state.find_matching_screen(current_hash, self.config.hash_threshold)
            if existing:
                screen_id = existing
                self.state.screens[screen_id].visit_count += 1
                consecutive_known += 1
                print(f"  [revisit] {self.state.screens[screen_id].screen_name} "
                      f"(visited {self.state.screens[screen_id].visit_count}x)")
            else:
                screen_id = current_hash
                consecutive_known = 0

                # New screen — analyze it
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

                # Save screenshot
                ss_path = self.state.output_dir / "screenshots" / f"{screen_id[:16]}.png"
                screenshot.save(ss_path)

                # Record the screen
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
                print(f"         → {analysis.screen_name}: {analysis.description[:80]}")

            # Update path
            if screen_id not in self.state.current_path:
                self.state.current_path.append(screen_id)

            # Decide what to do next
            if consecutive_known > 5:
                # Stuck in a loop — backtrack
                action = NavigationAction(action="back", reason="stuck in loop")
                consecutive_known = 0
            elif len(self.state.current_path) > self.config.max_depth:
                action = NavigationAction(action="back", reason="max depth reached")
            else:
                clickable = self.device.get_clickable_elements()
                elements_for_ai = [
                    {"label": e.label, "center": e.center, "class": e.class_name}
                    for e in clickable
                ]
                action = self.analyzer.decide_next_action(
                    screenshot,
                    elements_for_ai,
                    [s.screen_name for s in self.state.screens.values()],
                )

            # Execute the action
            prev_screen = screen_id
            self._execute_action(action)
            self.state.action_count += 1
            print(f"  [{self.state.action_count}] {action.action} → {action.reason}")

            time.sleep(self.config.settle_delay)

            # After action, check what screen we're on for edge recording
            post_screenshot = self.device.screenshot()
            post_hash = screen_hash(post_screenshot)
            post_screen = self.state.find_matching_screen(post_hash, self.config.hash_threshold)
            if post_screen and post_screen != prev_screen:
                self.state.graph.add_edge(
                    prev_screen, post_screen,
                    action=action.action,
                    reason=action.reason,
                )

            if action.action == "back" and self.state.current_path:
                self.state.current_path.pop()

        # Save crawl results
        self._save_results()
        print(f"\nCrawl complete: {len(self.state.screens)} screens, "
              f"{self.state.action_count} actions")
        return self.state

    def _execute_action(self, action: NavigationAction) -> None:
        """Execute a navigation action on the device."""
        match action.action:
            case "tap":
                self.device.tap(action.x, action.y)
            case "swipe_up":
                w, h = 540, 1920  # Approximate, could query device
                self.device.swipe(w // 2, h * 3 // 4, w // 2, h // 4)
            case "swipe_down":
                w, h = 540, 1920
                self.device.swipe(w // 2, h // 4, w // 2, h * 3 // 4)
            case "back":
                self.device.press_back()
            case "type":
                self.device.input_text(action.text)
            case _:
                self.device.press_back()

    def _save_results(self) -> None:
        """Save crawl state to JSON and Mermaid diagram."""
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
