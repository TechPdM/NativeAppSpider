# AppSpider Architecture

## Overview

AppSpider is an automated mobile app UI crawler. It connects to an Android device or emulator, systematically navigates through an app's screens, and produces structured documentation of every screen, element, and transition it discovers.

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
| `screenshot()` | Captures screen as PIL Image | `adb exec-out screencap -p` |
| `tap(x, y)` | Taps at pixel coordinates | `adb shell input tap` |
| `swipe(x1, y1, x2, y2)` | Swipes between two points | `adb shell input swipe` |
| `press_back()` | Android back button | `adb shell input keyevent 4` |
| `press_home()` | Android home button | `adb shell input keyevent 3` |
| `input_text(text)` | Types text into focused field | `adb shell input text` |
| `launch_app(package)` | Launches app by package name | `adb shell monkey -p ...` |
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

Given the current screenshot, clickable elements, and visited screen names, returns a single `NavigationAction` — the best next step to maximize exploration coverage. Falls back to "back" if the screen appears fully explored.

**Model:** Defaults to `claude-sonnet-4-6` for the balance of vision quality and cost. Each crawl step makes 1-2 API calls (analyze + decide), so a 50-screen crawl is roughly 100 API calls.

### `crawler.py` — Crawl Orchestrator

The central loop that ties everything together. Manages the state graph and drives exploration.

**Key classes:**

- `CrawlConfig` — all tunable parameters (max screens, max depth, max actions, settle delay, hash threshold)
- `CrawlState` — runtime state: the NetworkX directed graph, screen registry, action counter, current navigation path
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

    if stuck in loop (>5 consecutive revisits):
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

**Loop detection:** If the crawler visits already-known screens 5+ times in a row, it forces a back navigation to escape cycles (e.g., tapping a button that opens a dialog that immediately closes).

**Backtracking:** The crawler maintains a `current_path` stack. When it presses back, it pops the stack. This gives it a depth-first flavor within the broader exploration — it goes deep, backtracks, then tries alternate branches.

### `reporter.py` — Output Generation

Produces a self-contained HTML report from the crawl artifacts:

- **Screen cards:** Each screen gets a card with its screenshot (base64-embedded), name, description, element inventory (collapsible), activity name, and visit count
- **Flow diagram:** Mermaid.js graph rendered in-browser showing screen-to-screen transitions with action labels on edges
- **Stats:** Total screen and transition counts

The report has zero external dependencies besides a CDN-loaded Mermaid.js — it's a single `.html` file you can open anywhere.

### `cli.py` — Command-Line Interface

Built with Click. Two commands:

```
appspider crawl <package> [options]   # Run a crawl
appspider report <crawl-dir>          # Regenerate report from saved data
```

The `crawl` command auto-generates a report after the crawl completes.

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

Costs scale with the `max_actions` limit more than `max_screens`, since revisited screens still trigger `decide_next_action` calls. Using `claude-sonnet-4-6` (the default) balances vision quality with cost. Switch to `claude-haiku-4-5` for cheaper exploratory runs at some quality tradeoff.

## Future Directions

These are not planned — just areas where the PoC could grow:

- **iOS support** via `xcrun simctl` for Simulator control
- **Accessibility tree** integration alongside vision for richer element data
- **Diff mode** to compare two crawls of the same app (before/after a release)
- **Flow replay** to re-execute a recorded path for regression testing
- **Parallel exploration** with multiple emulator instances
- **Cost optimization** with a cheaper model for navigation and Claude for analysis only
