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
from nativeappspider.recorder import CrawlRecorder

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

# Labels that indicate a positive/accept dismiss action on app-level dialogs
# (consent banners, cookie popups, etc.). Matched as substrings, case-insensitive.
APP_DISMISS_LABELS = (
    "consent", "accept", "agree", "ok", "got it", "continue",
    "close", "dismiss", "skip", "not now", "no thanks", "later",
    "allow", "confirm",
)

# Package prefixes and resource ID patterns that indicate ad elements.
# These regions are masked before hashing so rotating ads don't make the
# same screen look like a new one.
AD_PACKAGE_PREFIXES = (
    "com.google.android.gms.ads",
    "com.google.android.gms.ad",
    "com.facebook.ads",
    "com.applovin",
    "com.unity3d.ads",
    "com.ironsource",
    "com.mopub",
    "com.inmobi",
    "com.chartboost",
    "com.vungle",
    "com.adcolony",
)

AD_RESOURCE_ID_PATTERNS = (
    "ad_view", "adview", "ad_banner", "adbanner", "ad_container",
    "adcontainer", "banner_ad", "bannerad", "interstitial",
    "native_ad", "nativead", "ad_frame", "adframe",
    "google_ads", "admob",
)

# How many times a screen can trigger a relaunch before it's marked toxic
TOXIC_RELAUNCH_THRESHOLD = 2

# Android widget class names that represent text input fields.
# These are excluded from navigation decisions (the crawler shouldn't
# tap into form fields) but still documented in screen analysis.
TEXT_INPUT_CLASSES = frozenset({
    "android.widget.EditText",
    "android.widget.AutoCompleteTextView",
    "android.widget.MultiAutoCompleteTextView",
    "android.inputmethodservice.ExtractEditText",
    "androidx.appcompat.widget.AppCompatEditText",
    "androidx.appcompat.widget.AppCompatAutoCompleteTextView",
    "com.google.android.material.textfield.TextInputEditText",
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
    dismiss_flows: list[str] = field(default_factory=list)
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
    focus_screen_id: str | None = None  # screen_id of the focus target once found
    # Track which specific elements have been tapped per screen, using
    # (bounds, label) tuples as structural identifiers. This enables precise
    # "all elements explored" detection without relying on Claude.
    screen_tapped_elements: dict[str, set[tuple]] = field(default_factory=dict)
    # Screens that have caused the app to leave (triggering relaunches).
    # Once a screen hits the threshold it's treated as toxic and auto-skipped.
    toxic_screen_counts: dict[str, int] = field(default_factory=dict)

    def find_matching_screen(self, hash_val: str, threshold: int = 12) -> str | None:
        """Find an existing screen that matches the given hash."""
        for sid in self.screens:
            if are_similar(sid, hash_val, threshold):
                return sid
        return None

    def find_screen_by_name(self, name: str) -> str | None:
        """Find an existing screen with the same name (case-insensitive)."""
        name_lower = name.lower()
        for sid, node in self.screens.items():
            if node.screen_name.lower() == name_lower:
                return sid
        return None


def load_checkpoint(crawl_dir: Path) -> tuple[CrawlState, CrawlConfig]:
    """Load a previous crawl's state and config from disk.

    Reads screens.json, transitions.json, and crawl_state.json to rebuild
    a CrawlState that can be passed to a Crawler for resumption.
    """
    crawl_dir = Path(crawl_dir)

    # Load screens
    screens_data = json.loads((crawl_dir / "screens.json").read_text())
    state = CrawlState()
    state.output_dir = crawl_dir

    name_to_sid: dict[str, str] = {}
    for sid, sdata in screens_data.items():
        node = ScreenNode(
            screen_id=sid,
            screen_name=sdata["screen_name"],
            description=sdata["description"],
            activity=sdata["activity"],
            elements=sdata["elements"],
            screenshot_path=sdata["screenshot"],
            visit_count=sdata.get("visit_count", 1),
            first_seen=sdata.get("first_seen", ""),
        )
        state.screens[sid] = node
        state.graph.add_node(sid, name=node.screen_name)
        name_to_sid[node.screen_name] = sid

    # Load transitions and rebuild graph edges
    transitions = json.loads((crawl_dir / "transitions.json").read_text())
    for edge in transitions:
        from_sid = name_to_sid.get(edge["from"])
        to_sid = name_to_sid.get(edge["to"])
        if from_sid and to_sid:
            state.graph.add_edge(
                from_sid, to_sid,
                action=edge.get("action", ""),
                reason=edge.get("reason", ""),
            )

    # Load runtime state from checkpoint
    checkpoint_path = crawl_dir / "crawl_state.json"
    if checkpoint_path.exists():
        cp = json.loads(checkpoint_path.read_text())
        state.action_count = cp.get("action_count", 0)
        state.screen_actions = cp.get("screen_actions", {})
        state.focus_reached = cp.get("focus_reached", False)
        state.focus_screen_id = cp.get("focus_screen_id")
        state.toxic_screen_counts = cp.get("toxic_screen_counts", {})

        # Restore screen_tapped_elements: list of [bounds, label] → set of (tuple, str)
        for sid, elements in cp.get("screen_tapped_elements", {}).items():
            state.screen_tapped_elements[sid] = {
                (tuple(e[0]), e[1]) for e in elements
            }

        config_data = cp.get("config", {})
    else:
        config_data = {}

    config = CrawlConfig(
        package=config_data.get("package", ""),
        max_screens=config_data.get("max_screens", 50),
        max_actions=config_data.get("max_actions", 200),
        max_depth=config_data.get("max_depth", 10),
        settle_delay=config_data.get("settle_delay", 1.5),
        output_dir=str(crawl_dir.parent),
        hash_threshold=config_data.get("hash_threshold", 12),
        avoid_flows=config_data.get("avoid_flows", []),
        dismiss_flows=config_data.get("dismiss_flows", []),
        focus_screen=config_data.get("focus_screen"),
        scroll_discovery=config_data.get("scroll_discovery", True),
    )

    return state, config


class Crawler:
    """Orchestrates the app crawling process."""

    def __init__(self, config: CrawlConfig, device: Device | None = None, model: str | None = None,
                 record: bool = False, resume_state: CrawlState | None = None):
        self.config = config
        self.device = device or Device()
        self.analyzer = Analyzer(model=model) if model else Analyzer()
        self.state = resume_state or CrawlState()
        self._record = record
        self._recorder: CrawlRecorder | None = None

    def crawl(self) -> CrawlState:
        """Run the main crawl loop."""
        self._screen_w, self._screen_h = self.device.get_screen_size()
        logger.info("Device screen: %dx%d", self._screen_w, self._screen_h)

        # Set up output directory — reuse existing dir on resume
        resuming = bool(self.state.screens)
        if resuming:
            # output_dir was already set by load_checkpoint
            (self.state.output_dir / "screenshots").mkdir(exist_ok=True)
            print(f"Resuming crawl: {len(self.state.screens)} screens, "
                  f"{self.state.action_count} actions so far")
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.state.output_dir = Path(self.config.output_dir) / f"{self.config.package}_{timestamp}"
            self.state.output_dir.mkdir(parents=True, exist_ok=True)
            (self.state.output_dir / "screenshots").mkdir(exist_ok=True)

        # Set up recorder if requested
        if self._record:
            config_dict = {
                "package": self.config.package,
                "max_screens": self.config.max_screens,
                "max_actions": self.config.max_actions,
                "max_depth": self.config.max_depth,
                "hash_threshold": self.config.hash_threshold,
                "avoid_flows": self.config.avoid_flows,
                "dismiss_flows": self.config.dismiss_flows,
                "focus_screen": self.config.focus_screen,
                "scroll_discovery": self.config.scroll_discovery,
            }
            self._recorder = CrawlRecorder(self.state.output_dir, config_dict)

        # Force-stop first to reset the task stack, ensuring we start
        # from the main activity regardless of where the app was left
        self.device.force_stop(self.config.package)

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

            # Auto-skip screens that have repeatedly caused the app to leave.
            # Press back immediately to avoid wasting actions on dead ends.
            if (
                not is_new
                and self.state.toxic_screen_counts.get(screen_id, 0) >= TOXIC_RELAUNCH_THRESHOLD
            ):
                name = self.state.screens[screen_id].screen_name
                print(f"  [toxic] Skipping '{name}' (causes relaunches)")
                self.device.press_back()
                self.state.action_count += 1
                time.sleep(self.config.settle_delay)
                continue

            # Fetch clickable elements once per iteration — reused by both the
            # screen analyzer (to document elements) and the action decider
            clickable = self.device.get_clickable_elements()

            # Start recording this iteration if recorder is active
            if self._recorder:
                ss_name = f"screenshots/{screen_id[:16]}.png"
                self._recorder.begin_step(
                    iteration=self.state.action_count + 1,
                    screenshot=screenshot,
                    screenshot_path=ss_name,
                    screen_id=screen_id,
                    is_new=is_new,
                    activity=self.device.current_activity(),
                    clickable=clickable,
                )

            if is_new:
                consecutive_known = 0
                try:
                    # Send the screenshot to Claude for analysis and documentation
                    result = self._process_new_screen(screenshot, screen_id, clickable)
                except Exception as e:
                    # If Claude fails (network, parsing, etc.), still record the
                    # screen so we don't keep trying to analyze it on revisits
                    logger.error("Failed to analyze screen: %s", e)
                    self._record_minimal_screen(screenshot, screen_id)
                    result = True

                # If the screen was avoided, press back and skip to next iteration
                if result is False:
                    self.device.press_back()
                    self.state.action_count += 1
                    time.sleep(self.config.settle_delay)
                    continue

                # Name-based dedup: result is the existing screen_id string.
                # Switch to it so the rest of the loop treats this as a revisit.
                if isinstance(result, str):
                    screen_id = result
                    consecutive_known += 1
                    # Fall through to action selection with the canonical screen_id
                else:
                    # If this new screen matches a dismiss flow, auto-dismiss it
                    # right away instead of exploring it further
                    if self._is_dismiss_screen(screen_id):
                        if self._auto_dismiss_app_dialog(clickable):
                            self.state.action_count += 1
                            time.sleep(self.config.settle_delay)
                            continue

                    # Scroll through any scrollable containers to discover
                    # off-screen elements that aren't visible in the initial viewport
                    if self.config.scroll_discovery:
                        clickable = self._discover_scrollable_elements(screen_id, clickable)
            else:
                self.state.screens[screen_id].visit_count += 1
                consecutive_known += 1
                print(f"  [revisit] {self.state.screens[screen_id].screen_name} "
                      f"(visited {self.state.screens[screen_id].visit_count}x)")

                # If this is a dismiss-flow screen, try to auto-dismiss it
                # instead of falling through to normal action selection
                if self._is_dismiss_screen(screen_id):
                    if self._auto_dismiss_app_dialog(clickable):
                        self.state.action_count += 1
                        time.sleep(self.config.settle_delay)
                        continue

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
                # Blame the screen we were on — it caused us to leave the app
                if screen_id in self.state.screens:
                    self.state.toxic_screen_counts[screen_id] = (
                        self.state.toxic_screen_counts.get(screen_id, 0) + 1
                    )
                    count = self.state.toxic_screen_counts[screen_id]
                    if count == TOXIC_RELAUNCH_THRESHOLD:
                        name = self.state.screens[screen_id].screen_name
                        print(f"  [toxic] '{name}' caused {count} relaunches, will auto-skip in future")
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

            # Decide what to do next — exclude text input fields so the
            # crawler doesn't waste actions tapping into form fields
            navigable = [
                e for e in clickable
                if e.class_name not in TEXT_INPUT_CLASSES
            ]
            action = self._decide_action(
                screenshot, screen_id, consecutive_known, navigable,
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

            # Finalize recording for this iteration
            if self._recorder:
                self._recorder.record_action(action)
                self._recorder.end_step()

            # Record which element was tapped so the per-element loop
            # detector knows this element has been tried on this screen
            if action.action == "tap":
                self._record_tapped_element(screen_id, action, clickable)

            time.sleep(self.config.settle_delay)

            # Save checkpoint for crash resilience
            self._save_checkpoint()

            # Pressing back pops the navigation stack so our depth tracking
            # stays in sync with the device's actual back stack
            if action.action == "back" and self.state.current_path:
                self.state.current_path.pop()

        # Save crawl results
        self._save_results()
        if self._recorder:
            self._recorder.save()
            print("Recording saved to recording.json")
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

    def _mask_ad_regions(self, screenshot: Image.Image) -> Image.Image:
        """Return a copy of the screenshot with ad regions painted over.

        Checks the UI hierarchy for elements belonging to known ad SDK
        packages or with ad-related resource IDs. Paints a solid grey
        rectangle over each match so the perceptual hash ignores them.
        Only copies the image if ads are actually found.
        """
        try:
            hierarchy = self.device.get_ui_hierarchy()
        except ADBError:
            return screenshot

        ad_bounds = []
        for e in hierarchy:
            is_ad = False
            # Check package name against known ad SDKs
            if e.package:
                pkg_lower = e.package.lower()
                if any(pkg_lower.startswith(prefix) for prefix in AD_PACKAGE_PREFIXES):
                    is_ad = True
            # Check resource ID for ad-related patterns
            if not is_ad and e.resource_id:
                rid_lower = e.resource_id.lower()
                if any(pat in rid_lower for pat in AD_RESOURCE_ID_PATTERNS):
                    is_ad = True

            if is_ad:
                x1, y1, x2, y2 = e.bounds
                if x2 > x1 and y2 > y1:  # valid non-zero bounds
                    ad_bounds.append((x1, y1, x2, y2))

        if not ad_bounds:
            return screenshot

        # Copy and mask — fill ad regions with solid grey
        from PIL import ImageDraw
        masked = screenshot.copy()
        draw = ImageDraw.Draw(masked)
        for bounds in ad_bounds:
            draw.rectangle(bounds, fill=(128, 128, 128))
        logger.debug("Masked %d ad region(s) before hashing", len(ad_bounds))
        return masked

    def _capture_and_identify(self) -> tuple[Image.Image, str, bool]:
        """Capture screenshot, hash it, return (image, screen_id, is_new).

        Before hashing, masks ad regions in the screenshot so that rotating
        ads don't cause the same screen to be treated as a new one.
        """
        screenshot = self.device.screenshot()

        # Mask ad regions so they don't affect the perceptual hash
        hash_image = self._mask_ad_regions(screenshot)
        current_hash = screen_hash(hash_image)

        existing = self.state.find_matching_screen(current_hash, self.config.hash_threshold)
        if existing:
            return screenshot, existing, False
        return screenshot, current_hash, True

    @staticmethod
    def _matches_flow_keywords(name: str, description: str, keywords: list[str]) -> bool:
        """Check if name or description contains any keyword (case-insensitive)."""
        if not keywords:
            return False
        name_lower = name.lower()
        desc_lower = description.lower()
        return any(
            kw.lower() in name_lower or kw.lower() in desc_lower
            for kw in keywords
        )

    def _is_avoided_screen(self, analysis: ScreenAnalysis) -> bool:
        """Check if a screen matches any of the avoid flows."""
        return self._matches_flow_keywords(
            analysis.screen_name, analysis.description, self.config.avoid_flows,
        )

    def _is_dismiss_screen(self, screen_id: str) -> bool:
        """Check if a recorded screen matches any dismiss flow keywords."""
        node = self.state.screens.get(screen_id)
        if not node:
            return False
        return self._matches_flow_keywords(
            node.screen_name, node.description, self.config.dismiss_flows,
        )

    def _auto_dismiss_app_dialog(self, clickable: list) -> bool:
        """Try to dismiss an app-level dialog by tapping a dismiss button.

        Scans clickable elements for labels that look like dismiss/accept
        buttons (e.g. "Consent", "Close", "OK"). Also looks for small
        close/X buttons in the top-right quadrant of the screen.

        Returns True if a dismiss action was taken.
        """
        # First pass: look for labeled dismiss buttons
        for e in clickable:
            label = e.label.lower() if e.label else ""
            if not label:
                continue
            for dismiss_label in APP_DISMISS_LABELS:
                if dismiss_label in label:
                    cx, cy = e.center
                    print(f"  [dismiss] Tapping '{e.label}' to dismiss dialog")
                    self.device.tap(cx, cy)
                    return True

        # Second pass: look for a small close/X button in the top-right
        # (common pattern for dialog overlays)
        w = self._screen_w
        for e in clickable:
            cx, cy = e.center
            x1, y1, x2, y2 = e.bounds
            button_w = x2 - x1
            button_h = y2 - y1
            # Small button (< 150px) in the right half, top third of screen
            if (button_w < 150 and button_h < 150
                    and cx > w * 0.6 and cy < self._screen_h * 0.33):
                label = e.label or "X"
                print(f"  [dismiss] Tapping close button '{label}' at ({cx},{cy})")
                self.device.tap(cx, cy)
                return True

        return False

    def _process_new_screen(
        self, screenshot: Image.Image, screen_id: str, clickable: list,
    ) -> bool | str:
        """Analyze and record a new screen.

        Returns True if the screen was recorded, False if it was skipped
        because it matches an avoided flow, or the existing screen_id string
        if this screen was deduplicated against an existing screen by name.
        """
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
            dismiss_flows=self.config.dismiss_flows or None,
            focus_screen=active_focus,
        )

        if self._recorder:
            self._recorder.record_analysis(analysis)

        # Skip recording screens that match avoided flows
        if self._is_avoided_screen(analysis):
            print(f"  [avoid] Skipping screen: {analysis.screen_name}")
            return False

        # Name-based deduplication: if Claude gave this screen the same name
        # as an existing screen, treat it as a revisit (e.g. form fields in
        # different focus states producing different hashes but same screen)
        existing_sid = self.state.find_screen_by_name(analysis.screen_name)
        if existing_sid:
            existing = self.state.screens[existing_sid]
            existing.visit_count += 1
            print(f"  [dedup] '{analysis.screen_name}' matches existing screen, "
                  f"treating as revisit (visited {existing.visit_count}x)")
            return existing_sid

        # Check if this screen matches the focus target
        if (
            self.config.focus_screen
            and not self.state.focus_reached
            and analysis.matches_focus_target
        ):
            self.state.focus_reached = True
            self.state.focus_screen_id = screen_id
            print(f"  [focus] Reached target screen: {analysis.screen_name}")

        self._record_screen(screenshot, screen_id, analysis)
        print(f"         → {analysis.screen_name}: {analysis.description[:80]}")
        return True

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

        # Breadth-first from focus screen: when we're back on the focus
        # target, pick the next untried element directly instead of asking
        # Claude (which tends to re-explore the same deep path).
        if (
            self.state.focus_screen_id
            and screen_id == self.state.focus_screen_id
            and tapped  # only after first visit (tapped is non-empty)
        ):
            untried = [
                e for e in clickable
                if (e.bounds, e.label) not in tapped
            ]
            if untried:
                pick = untried[0]
                cx, cy = pick.center
                label = pick.label or "element"
                return NavigationAction(
                    action="tap", x=cx, y=cy,
                    reason=f"breadth-first: trying untried '{label}' on focus screen",
                )

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
                dismiss_flows=self.config.dismiss_flows or None,
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

        self._save_checkpoint()
        print(f"Results saved to {out}/")

    def _save_checkpoint(self) -> None:
        """Save runtime state to crawl_state.json for crash recovery and resume."""
        # Serialize screen_tapped_elements: set of (tuple, str) → list of [list, str]
        tapped_serial = {}
        for sid, elements in self.state.screen_tapped_elements.items():
            tapped_serial[sid] = [
                [list(bounds), label] for bounds, label in elements
            ]

        checkpoint = {
            "action_count": self.state.action_count,
            "screen_actions": self.state.screen_actions,
            "screen_tapped_elements": tapped_serial,
            "toxic_screen_counts": self.state.toxic_screen_counts,
            "focus_reached": self.state.focus_reached,
            "focus_screen_id": self.state.focus_screen_id,
            "config": {
                "package": self.config.package,
                "max_screens": self.config.max_screens,
                "max_actions": self.config.max_actions,
                "max_depth": self.config.max_depth,
                "settle_delay": self.config.settle_delay,
                "hash_threshold": self.config.hash_threshold,
                "avoid_flows": self.config.avoid_flows,
                "dismiss_flows": self.config.dismiss_flows,
                "focus_screen": self.config.focus_screen,
                "scroll_discovery": self.config.scroll_discovery,
            },
        }

        # Atomic write to avoid corruption on crash
        out = self.state.output_dir
        tmp_path = out / "crawl_state.json.tmp"
        tmp_path.write_text(json.dumps(checkpoint, indent=2))
        tmp_path.rename(out / "crawl_state.json")
