# NativeAppSpider Architecture

## Overview

NativeAppSpider is an automated mobile app UI crawler. It connects to an Android device or emulator, systematically navigates through an app's screens, and produces structured documentation of every screen, element, and transition it discovers.

The core idea: treat an app's UI as a **directed graph** where screens are nodes and user actions are edges, then explore that graph using an AI-guided breadth-first strategy.

```
┌─────────────┐     screenshots      ┌──────────────┐
│   Android    │ ──────────────────→  │   Analyzer    │
│   Device     │                      │  (Claude AI)  │
│  (via ADB)   │ ←──────────────────  │              │
└─────────────┘   tap/swipe/back      └──────┬───────┘
       │                                     │
       │  UI hierarchy XML                   │  screen analysis +
       │  activity name                      │  next action decision
       │                                     │
       ▼                                     ▼
┌─────────────────────────────────────────────────────┐
│                     Crawler                          │
│                                                      │
│  ┌─────────┐   ┌────────────┐   ┌────────────────┐  │
│  │ Hasher  │   │ State Graph│   │ Action Execute │  │
│  │ (dedup) │   │ (NetworkX) │   │ (dispatch)     │  │
│  └─────────┘   └────────────┘   └────────────────┘  │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│                    Reporter                          │
│                                                      │
│  screens.json  transitions.json  flow.mmd  report.html│
└─────────────────────────────────────────────────────┘
```

## Module Breakdown

### `device.py` — ADB Device Interface

Wraps Android Debug Bridge (ADB) commands into a Python interface. All device interaction goes through this module.

**Key class: `Device`**

| Method | What it does | ADB command |
|---|---|---|
| `is_connected()` | Checks if a device is reachable | `adb devices` |
| `get_screen_size()` | Returns (width, height), prefers override | `adb shell wm size` |
| `screenshot()` | Captures screen as PIL Image | `adb exec-out screencap -p` |
| `tap(x, y)` | Taps at pixel coordinates | `adb shell input tap` |
| `swipe(x1, y1, x2, y2)` | Swipes between two points | `adb shell input swipe` |
| `press_back()` | Android back button | `adb shell input keyevent 4` |
| `press_home()` | Android home button | `adb shell input keyevent 3` |
| `input_text(text)` | Types text into focused field | `adb shell input text` |
| `launch_app(package)` | Launches app by package name | `adb shell am start` (monkey fallback) |
| `force_stop(package)` | Kill app and clear task stack | `adb shell am force-stop` |
| `clear_app_data(package)` | Wipe app data (fresh install) | `adb shell pm clear` |
| `is_package_installed(package)` | Check if app is installed | `adb shell pm list packages` |
| `current_activity()` | Gets foreground activity name | `adb shell dumpsys activity` |
| `get_ui_hierarchy()` | Dumps UI tree as `UIElement` list | `adb shell uiautomator dump` |
| `get_clickable_elements()` | Filters hierarchy to clickable elements | (filters `get_ui_hierarchy()`) |

**Key data class: `UIElement`**

Represents a single node in Android's view hierarchy. Carries the element's `resource_id`, `class_name`, `text`, `content_desc`, `bounds`, and boolean flags (`clickable`, `scrollable`, `enabled`). Provides a `center` property for tap coordinates and a `label` property that picks the best human-readable identifier.

### `hasher.py` — Screen Deduplication

Determines whether two screenshots represent the same logical screen. This is critical — without it the crawler would treat every frame as a new screen (clocks tick, animations play, battery drains).

**Algorithm: Perceptual Average Hash**

1. Resize screenshot to a small grid (default 16x16)
2. Convert to grayscale
3. Compute mean pixel value
4. Each pixel becomes 1 (above mean) or 0 (below) — producing a 256-bit hash
5. Two hashes are "similar" if their Hamming distance is below a threshold (default 12)

Uses the `imagehash` library. The threshold of 12 out of 256 bits (~5% tolerance) handles status bar changes while still distinguishing meaningfully different screens.

### `analyzer.py` — Claude AI Integration

Sends screenshots to Claude's vision API for two purposes:

**1. Screen Analysis (`analyze_screen`)**

Given a screenshot (and optionally the UI hierarchy + crawl context), returns a `ScreenAnalysis`:
- `screen_name` — short identifier (e.g. "Settings", "Login Form")
- `description` — what the screen is for
- `elements` — list of interactive elements with label, type, and purpose
- `suggested_actions` — prioritized list of actions to explore new screens

The prompt includes context about already-visited screens so the AI can prioritize unexplored paths.

**2. Navigation Decision (`decide_next_action`)**

Given the current screenshot, clickable elements, and visited screen names, returns a single `NavigationAction` — the best next step to maximize exploration coverage. Falls back to "back" if the screen appears fully explored. Accepts additional context: `avoid_flows`, `dismiss_flows`, `focus_screen`, `recent_actions` (per-screen action history to prevent repeat taps), and `target_package` (to keep the crawler inside the app).

**Key data classes:**

- `ScreenAnalysis` — `screen_name`, `description`, `elements`, `suggested_actions`, `matches_focus_target` (semantic judgment of whether the screen matches a `--focus` target)
- `NavigationAction` — `action` (tap/swipe_up/swipe_down/back/type), `x`, `y`, `text`, `reason`

**Model:** Defaults to `claude-sonnet-4-6` for both calls. Analysis and navigation can use different models via `--analysis-model` and `--decision-model` to trade quality for cost. Each crawl step makes 1-2 API calls (analyze + decide), so a 50-screen crawl is roughly 100 API calls.

### `crawler.py` — Crawl Orchestrator

The central loop that ties everything together. Manages the state graph and drives exploration.

**Key classes:**

- `CrawlConfig` — all tunable parameters (max screens, max depth, max actions, settle delay, hash threshold, avoid/dismiss/focus flows, scroll discovery)
- `CrawlState` — runtime state: the NetworkX directed graph, screen registry, action counter, current navigation path, per-screen action history (`screen_actions`), per-element tap tracking (`screen_tapped_elements`), toxic screen counts, focus state
- `ScreenNode` — a discovered screen with its analysis, activity name, screenshot path, and visit count
- `Crawler` — the orchestrator

**Crawl Loop (simplified):**

```
launch app
while under limits:
    screenshot = device.screenshot()
    hash = perceptual_hash(screenshot)

    if hash matches known screen:
        mark as revisit
    else:
        analyze with Claude → get screen name, elements, description
        save screenshot
        add node to graph

    if stuck in loop (>10 consecutive revisits):
        press back
    elif at max depth:
        press back
    else:
        ask Claude for next action

    execute action (tap/swipe/back/type)
    wait for screen to settle

    screenshot again → record edge if screen changed

save results
```

**Termination conditions:**
- `max_actions` reached (default 200)
- `max_screens` discovered (default 50)
- Max consecutive screenshot failures (10) — breaks cleanly if the device becomes unresponsive

**Loop detection (two levels):**

1. **Per-element tracking** — each tap is recorded as a `(bounds, label)` tuple in `screen_tapped_elements`. When all clickable elements on a screen have been tapped, the crawler forces a back navigation without making an API call. On `--focus` screens, this enables breadth-first exploration: untried elements are picked before asking Claude.
2. **Consecutive revisit safety net** — if the crawler visits already-known screens 10+ times in a row, it forces a back navigation to escape cycles (e.g., tapping a button that opens a dialog that immediately closes).

**Backtracking:** The crawler maintains a `current_path` stack. When it presses back, it pops the stack. This gives it a depth-first flavor within the broader exploration — it goes deep, backtracks, then tries alternate branches.

**Toxic screen detection:** Screens that repeatedly cause the app to leave (triggering relaunches) are tracked in `toxic_screen_counts`. After a screen triggers 2+ relaunches, it's marked toxic and auto-skipped — the crawler immediately presses back instead of trying to interact with it.

**Name-based deduplication:** When Claude assigns the same name to a visually different screen (e.g., form states, list scroll positions), `find_screen_by_name()` treats them as revisits rather than consuming the screen budget. Final output gets numeric suffixes via `_deduplicate_screen_names()` to keep names unique.

**Ad masking:** Before hashing, `_mask_ad_regions()` detects ad elements by package prefix (AdMob, Facebook Ads, Unity Ads, etc.) and resource ID patterns, then fills those regions with grey. This prevents rotating ad content from making the same screen appear as a new one.

**System dialog handling:** The crawler auto-detects system dialog overlays (permission prompts, "app not responding", etc.) by checking for elements from known system packages (`com.android.permissioncontroller`, `android`, etc.). When detected, it taps a dismiss button or presses back instead of analyzing the dialog as a screen.

**Scroll discovery:** When `scroll_discovery` is enabled (default), the crawler scrolls through scrollable containers to reveal off-screen elements. Stops when no new elements appear or after 5 scrolls per container.

**App escape recovery:** Each step checks `current_activity()` against the target package. If the crawler has escaped the app (e.g., tapped a link that opened a browser), it force-stops and relaunches. A max relaunch limit prevents infinite loops.

**Checkpoint saves:** After every iteration, the crawler writes `crawl_state.json` to the output directory (atomic write via tmp+rename). This enables crash-resilient `--continue` resumption.

### `reporter.py` — Output Generation

Produces a self-contained HTML report from the crawl artifacts:

- **Screen cards:** Each screen gets a card with its screenshot (base64-embedded), name, description, element inventory (collapsible), activity name, and visit count
- **Flow diagram:** Mermaid.js graph rendered in-browser showing screen-to-screen transitions with action labels on edges
- **Stats:** Total screen and transition counts

The report has zero external dependencies besides a CDN-loaded Mermaid.js — it's a single `.html` file you can open anywhere.

### `cli.py` — Command-Line Interface

Built with Click. Two commands:

```
nativeappspider crawl <package> [options]   # Run a crawl
nativeappspider report <crawl-dir>          # Regenerate report from saved data
```

The `crawl` command auto-generates a report after the crawl completes.

**Crawl options:**

| Flag | Default | Description |
|---|---|---|
| `--config <file>` | — | YAML config file (CLI args override file values) |
| `--max-screens` | 50 | Maximum unique screens to discover |
| `--max-actions` | 200 | Maximum actions to take |
| `--max-depth` | 10 | Max navigation depth before backtracking |
| `--output` | `output` | Output directory |
| `--serial` | — | ADB device serial (multi-device setups) |
| `--delay` | 1.5 | Seconds to wait after each action |
| `--model` | `claude-sonnet-4-6` | Model for both analysis and decisions |
| `--analysis-model` | — | Model for screen analysis only |
| `--decision-model` | — | Model for navigation decisions only |
| `--fresh` | off | Clear app data before crawling |
| `--avoid <flow>` | — | Skip matching screens (repeatable) |
| `--dismiss <screen>` | — | Auto-dismiss matching screens (repeatable) |
| `--focus <screen>` | — | Navigate to this screen first, then explore |
| `--scroll-discovery` | on | Scroll containers to find off-screen elements |
| `--record` | off | Capture crawl steps for replay test fixtures |
| `--continue <dir>` | — | Resume a previous crawl from its output dir |

**YAML config files** (`--config`) accept the same keys as CLI flags (snake_case). CLI arguments override config file values. Example:

```yaml
package: com.example.app
max_screens: 30
delay: 2.0
avoid: [login, registration]
dismiss: [consent, privacy]
focus: settings
```

## Data Flow

### During Crawl

```
Device ──screenshot──→ Hasher ──hash──→ Crawler (known screen?)
                                            │
                                     new ───┤──── known
                                     │           │
                              Analyzer          increment visit_count
                              (Claude)
                                     │
                              ScreenAnalysis
                                     │
                              save to graph ──→ Analyzer.decide_next_action
                                                        │
                                                 NavigationAction
                                                        │
                                                 Device.tap/swipe/back
```

### Output Artifacts

```
output/<package>_<timestamp>/
├── screenshots/
│   ├── <hash1>.png
│   ├── <hash2>.png
│   └── ...
├── screens.json          # All screen data (name, description, elements, activity)
├── transitions.json      # Edge list [{from, to, action, reason}]
├── flow.mmd              # Mermaid diagram source
├── crawl_state.json      # Checkpoint for --continue resumption (atomic writes)
├── recording.json        # (only with --record) Step-by-step crawl recording
└── report.html           # Self-contained visual report
```

## State Graph Model

The app's UI is modeled as a **directed multigraph** (NetworkX `DiGraph`):

- **Nodes** = unique screens, identified by perceptual hash
  - Attributes: `screen_name`, `description`, `activity`, `elements`, `screenshot_path`
- **Edges** = actions that transition between screens
  - Attributes: `action` (tap/swipe/back), `reason` (why the AI chose it)

This model naturally represents:
- Linear flows (onboarding, checkout)
- Tab navigation (multiple edges from a hub screen)
- Modal dialogs (screen → dialog → same screen)
- Dead ends (screens with no outgoing edges besides "back")

## Black-Box Approach

NativeAppSpider requires **no access to app source code**. It works on any app installed on a connected device or emulator, the same way a human tester would — by looking at the screen and tapping.

**Why this works without source code:**
- **ADB** operates at the OS level — screenshots, taps, and UI hierarchy dumps work on any app regardless of who built it
- **`uiautomator dump`** reads the accessibility tree exposed by the Android framework, not app internals
- **Claude** analyzes screenshots visually — it identifies buttons, text, and navigation elements from pixels
- **App launching** uses the package name, which is public (`adb shell pm list packages` lists all installed apps)

**Limitations of the black-box approach:**
- **Login/auth** — the crawler can't generate valid credentials. They must be provided as config, or the crawl is limited to pre-login screens.
- **Deep links** — without knowing the app's intent filters, the crawler can't jump directly to deep screens. It must navigate there through the UI.
- **Obfuscated element IDs** — ProGuard/R8 can strip meaningful resource IDs, so `uiautomator dump` may show `com.app:id/a1` instead of `com.app:id/login_button`. This has minimal impact since Claude reads the screenshot visually rather than relying on IDs.
- **Server-driven UI** — screens that require specific backend state (e.g., "order in progress") won't appear unless that state exists on the account being crawled.

None of these are blockers — they limit crawl depth on certain apps but don't prevent the tool from working.

## Key Design Decisions

### ADB over Appium
Appium adds a WebDriver server, session management, and cross-platform abstraction. For a PoC targeting Android only, raw ADB is simpler — fewer moving parts, no server to manage, and the commands map 1:1 to what we need.

### Perceptual Hash over Pixel Comparison
Exact pixel comparison would flag every screenshot as unique due to the status bar clock, battery icon, and animation frames. Perceptual hashing tolerates these minor differences while still distinguishing genuinely different screens.

### AI-Guided over Exhaustive Exploration
A brute-force crawler would tap every clickable element on every screen. This is thorough but slow and hits combinatorial explosion with list items, keyboards, etc. Using Claude to prioritize "interesting" elements (navigation links, menu items, tabs) over repetitive ones (list item #47) keeps the crawl focused and efficient.

### Two-Phase Screen Processing
Each new screen gets two AI calls: `analyze_screen` (document it) and `decide_next_action` (navigate away from it). Splitting these keeps each prompt focused and the responses structured. The analysis prompt can be detailed; the navigation prompt is kept terse for speed.

### Settle Delay
Mobile UIs have animations, lazy loading, and async data fetching. The configurable `settle_delay` (default 1.5s) gives screens time to reach a stable state before screenshotting. Too short = partial screenshots. Too long = slow crawls.

## Cost Considerations

Each crawl step involves 1-2 Claude API calls with image input. Rough estimates:

| Scenario | API Calls | Approximate Cost |
|---|---|---|
| Small app (10 screens) | ~20-30 | ~$0.50-1.00 |
| Medium app (30 screens) | ~80-120 | ~$2-4 |
| Large app (50 screens, 200 actions) | ~150-250 | ~$5-10 |

Costs scale with the `max_actions` limit more than `max_screens`, since revisited screens still trigger `decide_next_action` calls.

### Model Selection

The crawler uses two separate API calls per step, and each can use a different model:

- **Screen analysis** (`analyze_screen`) — documents new screens with names, descriptions, and element inventories. Requires strong vision understanding. Default: `claude-sonnet-4-6`.
- **Navigation decisions** (`decide_next_action`) — picks which element to tap next. Simpler task, called more frequently. Default: `claude-sonnet-4-6`.

By default both use the same model. To reduce costs, use `--decision-model claude-haiku-4-5` to switch navigation decisions to a cheaper model while keeping analysis on Sonnet. This can cut costs ~30-40% since navigation calls are roughly half of all API calls.

```bash
# Default: sonnet for both (best quality)
nativeappspider crawl com.example.app

# Cost-optimised: sonnet for analysis, haiku for decisions
nativeappspider crawl com.example.app --decision-model claude-haiku-4-5

# Budget: haiku for everything
nativeappspider crawl com.example.app --model claude-haiku-4-5

# Focus on a specific screen, avoid auth flows
nativeappspider crawl com.example.app --focus settings --avoid login

# Resume a previous crawl with higher budget
nativeappspider crawl --continue output/com.example.app_20240101_120000 --max-actions 400

# Use a YAML config file
nativeappspider crawl --config myapp.yaml
```

## Testing Architecture

Three tiers of tests, each with different trade-offs:

### Unit Tests (`tests/unit/`) — 71 tests

Fast, fully mocked, no device or API key needed. Each module has its own test file. Subprocess calls, HTTP requests, and file I/O are all mocked. Run constantly during development (~1s).

### Replay Integration Tests (`tests/integration/`) — 35 tests

Run the real `Crawler` code end-to-end but with scripted device and analyzer responses. Two mock classes in `tests/integration/replay.py` make this possible:

- **`ReplayDevice`** — serves a pre-defined sequence of `DeviceStep`s (screenshot, clickable elements, activity name). Actions (tap, swipe, back) advance to the next step but don't touch a real device.
- **`ReplayAnalyzer`** — serves a pre-defined sequence of `AnalyzerStep`s (screen analysis, navigation action). No API calls.

Each test scenario defines a list of steps, wires up the mocks, runs `crawler.crawl()`, and asserts on the resulting state graph, output files, and action history. This covers the full pipeline — hashing, graph building, loop detection, backtracking, dedup — without external dependencies.

**Total: 106 tests** (71 unit + 35 integration), all passing in ~2s.

### Fixture-Based Replay Tests (`tests/fixtures/`)

Replay tests using data captured from real crawls rather than hand-crafted synthetic data. The workflow:

1. **Record** — run a crawl with `--record` to capture each step (screenshot, UI elements, Claude analysis, action chosen) to `recording.json`
2. **Extract** — run `tests/fixtures/extract_fixture.py` to downscale screenshots and package into a compact fixture directory
3. **Replay** — `load_fixture()` in `replay.py` reads the fixture and returns `ReplayDevice` + `ReplayAnalyzer` + `CrawlConfig`

Fixtures are checked into the repo. The current fixture uses the Android Settings app (built into every emulator, no commercial data concerns). Screenshots are downscaled to 270x480 to keep the repo small (~97 KB per fixture).

```
tests/
├── conftest.py                 # Shared pytest fixtures
├── fixtures/
│   ├── extract_fixture.py      # Recording → fixture converter
│   └── settings/               # Android Settings app fixture
│       ├── scenario.json       # Steps with elements, analysis, actions
│       └── screenshots/        # Downscaled PNGs
├── integration/
│   ├── replay.py               # ReplayDevice, ReplayAnalyzer, load_fixture
│   └── test_crawl_replay.py    # All integration tests (synthetic + fixture)
└── unit/
    ├── test_analyzer.py
    ├── test_crawler.py
    ├── test_device.py
    ├── test_hasher.py
    └── test_reporter.py
```

### The `CrawlRecorder` (`src/nativeappspider/recorder.py`)

An opt-in recorder that hooks into the crawl loop when `--record` is passed. At each iteration it captures:

- Screenshot path and screen ID (perceptual hash)
- Whether the screen was new or a revisit
- Clickable elements (serialised `UIElement` data)
- `ScreenAnalysis` from Claude (for new screens)
- `NavigationAction` chosen

Writes `recording.json` to the crawl output directory. This file is the source of truth for fixture extraction — it captures the exact sequence of events as they happened, unlike `screens.json`/`transitions.json` which only store the final state.

## Future Directions

These are not planned — just areas where the PoC could grow:

- **iOS support** via `xcrun simctl` for Simulator control
- **Accessibility tree** integration alongside vision for richer element data
- **Diff mode** to compare two crawls of the same app (before/after a release)
- **Flow replay** to re-execute a recorded path for regression testing
- **Parallel exploration** with multiple emulator instances
- **Smart content wait** — compare consecutive screenshots to detect when a screen has finished loading, instead of relying on a fixed delay
