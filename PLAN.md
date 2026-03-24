# AppSpider Development Plan

## Current Status Assessment

The project has a complete architectural scaffold — all modules exist, imports resolve, the CLI runs, and the crawl loop logic is coherent. However, the code has never been executed against a real device. Several critical issues would cause immediate failures on first run.

### What's Done

| Component | Status | Notes |
|---|---|---|
| Project structure | Done | `pyproject.toml`, package layout, CLI entry point |
| Architecture docs | Done | `ARCHITECTURE.md`, `CLAUDE.md` |
| CLI (`cli.py`) | Done | `crawl` and `report` commands with Click |
| Device interface (`device.py`) | Partial | All methods exist but no error handling, screenshot method has a bug (duplicate ADB call, no temp cleanup) |
| Perceptual hasher (`hasher.py`) | Done | Average hash + similarity check, solid |
| Screen analyzer (`analyzer.py`) | Partial | Prompts written, JSON parsing works, but no API key validation, no retry logic, no response structure validation |
| Crawl loop (`crawler.py`) | Partial | Core loop logic complete, state graph works, but no error recovery, hardcoded device dimensions, no crash resilience |
| Reporter (`reporter.py`) | Done | HTML generation with Mermaid, self-contained output |
| Tests | Not started | Empty `tests/` directory |
| Error handling | Not started | No validation, no retries, no graceful failures anywhere |

### Critical Bugs to Fix First

1. **`device.py` screenshot()** — duplicate ADB call (line 61 result is ignored), no temp file cleanup, no validation of captured image
2. **No ADB error checking** — all `_run()` calls ignore return codes, failures are silent
3. **Hardcoded 540x1920 display size** in `crawler.py` swipe actions
4. **No API key check** — crashes with cryptic `AuthenticationError` if `ANTHROPIC_API_KEY` unset
5. **No device connectivity check** — crawl starts and immediately crashes if no emulator running

---

## Development Phases

### Phase 1: Make It Actually Run
**Goal:** Fix bugs and add minimum error handling so a crawl completes against a real emulator.

- [ ] **1.1 Fix screenshot method** — remove duplicate ADB call, add temp file cleanup, validate image before returning
- [ ] **1.2 Add ADB error checking** — check return codes in `_run()`, raise clear exceptions on failure
- [ ] **1.3 Add device connectivity check** — verify ADB connection and device availability before crawl starts (`adb devices`)
- [ ] **1.4 Add API key validation** — check `ANTHROPIC_API_KEY` is set at CLI startup, fail with clear message
- [ ] **1.5 Detect device dimensions** — query actual screen size via `adb shell wm size`, use for swipe coordinates
- [ ] **1.6 Add app launch verification** — confirm the target package exists and launched (check foreground activity matches package)
- [ ] **1.7 Wrap crawl loop in try/except** — catch and log errors per-step instead of crashing the whole crawl
- [ ] **1.8 Add API retry with backoff** — retry failed Claude calls (rate limits, timeouts) with exponential backoff
- [ ] **1.9 Validate AI response structure** — check required fields exist in JSON before using, handle missing x/y coordinates

### Phase 2: Test Against Real Apps
**Goal:** Run the crawler against 2-3 real apps, fix issues that emerge.

- [ ] **2.1 Set up Android emulator** — document emulator setup steps, test ADB connectivity
- [ ] **2.2 First crawl: simple app** — pick a Settings-like app with few screens, verify full pipeline
- [ ] **2.3 Fix issues from first crawl** — expect screen detection problems, timing issues, navigation dead ends
- [ ] **2.4 Second crawl: medium app** — app with tabs, lists, modals — stress test the state graph
- [ ] **2.5 Tune hash threshold** — adjust perceptual hash sensitivity based on real screenshot data
- [ ] **2.6 Tune settle delay** — find minimum reliable delay for different app types
- [ ] **2.7 Third crawl: complex app** — app with login, scrollable content, nested navigation
- [ ] **2.8 Review and improve AI prompts** — refine analysis and navigation prompts based on real Claude responses

### Phase 3: Robustness & Quality
**Goal:** Add tests, handle edge cases, make output reliable.

- [ ] **3.1 Unit tests for hasher** — test hash generation, similarity comparison, threshold boundaries
- [ ] **3.2 Unit tests for device** — mock ADB subprocess calls, test XML parsing, test UIElement extraction
- [ ] **3.3 Unit tests for analyzer** — mock Claude API, test JSON parsing, test fallback on malformed responses
- [ ] **3.4 Unit tests for crawler** — mock device + analyzer, test loop termination, backtracking, edge recording
- [ ] **3.5 Integration test with recorded data** — replay saved screenshots + UI dumps through the crawler without a real device
- [ ] **3.6 Handle orientation changes** — detect landscape/portrait, adjust coordinates
- [ ] **3.7 Handle system dialogs** — permission prompts, "app not responding", keyboard popups
- [ ] **3.8 Handle scroll discovery** — detect scrollable containers, scroll to reveal off-screen elements
- [ ] **3.9 Improve loop detection** — track per-element tap history, not just screen revisit count
- [ ] **3.10 Add crawl resume** — save state to disk periodically, allow resuming after crash

### Phase 4: Better Output
**Goal:** Make the generated documentation genuinely useful.

- [ ] **4.1 Annotated screenshots** — overlay element labels/bounds on screenshots
- [ ] **4.2 Richer HTML report** — clickable flow diagram (click node → scroll to screen card), filtering, search
- [ ] **4.3 Markdown export** — generate a Markdown version of the report for docs/wikis
- [ ] **4.4 JSON API schema** — formalize the output JSON schema so other tools can consume it
- [ ] **4.5 Screen grouping** — cluster related screens (e.g., all Settings subscreens) in the report
- [ ] **4.6 Diff reports** — compare two crawls of the same app, highlight new/removed/changed screens

### Phase 5: Expand Platform & Intelligence
**Goal:** iOS support, smarter crawling, cost optimization.

- [ ] **5.1 iOS Simulator support** — add `xcrun simctl` device backend alongside ADB
- [ ] **5.2 Abstract device interface** — extract `Device` into a protocol/ABC, implement Android and iOS backends
- [ ] **5.3 Smarter exploration strategy** — use Claude to build a mental model of the app's structure, prioritize unexplored areas
- [ ] **5.4 Cost optimization** — use a cheaper model (Haiku) for navigation decisions, reserve Sonnet/Opus for screen analysis
- [ ] **5.5 Parallel crawling** — run multiple emulator instances for different app sections simultaneously
- [ ] **5.6 Login/auth handling** — support providing credentials to crawl past login screens
- [ ] **5.7 Deep link exploration** — use `adb shell am start` with intent URIs to jump directly to deep screens

---

## Priority Order

**Do Phase 1 first** — nothing else matters until the tool can complete a real crawl without crashing. Most of Phase 1 is small fixes (a few hours of work).

**Phase 2 is where the real learning happens** — real apps will expose assumptions in the prompts, timing, and navigation strategy that can't be found by reading code. Expect to iterate heavily here.

**Phase 3 and 4 can be done in parallel** — tests and output improvements are independent workstreams.

**Phase 5 is stretch** — only pursue after Phases 1-3 are solid.

---

## Dependencies & External Tools

### Required

| Dependency | What it provides | Install |
|---|---|---|
| **ADB (Android Debug Bridge)** | All device communication — screenshots, taps, UI hierarchy dumps, app launching | Comes with Android SDK Platform Tools (~100MB) or Android Studio (~3GB) |
| **Android Emulator or device** | Something to crawl against | Android Studio AVD Manager, or standalone `sdkmanager` + `emulator` |
| **Anthropic API key** | Claude vision API for screen analysis and navigation decisions | `export ANTHROPIC_API_KEY=...` |
| **Python 3.12+** | Runtime | System install or pyenv |

### Optional: Appium

The current implementation uses raw ADB subprocess calls for all device interaction. This is the simplest approach with the fewest moving parts, but it has known limitations:

**Where raw ADB is sufficient:**
- Screenshot capture (binary pipe via `exec-out screencap`)
- Coordinate-based taps and swipes (Claude provides pixel coordinates from screenshots)
- Back/home button presses
- App launching
- Basic UI hierarchy dumps (`uiautomator dump`)

**Where raw ADB gets fragile:**
- **Text input** — `adb shell input text` breaks on special characters, unicode, and sometimes spaces
- **UI hierarchy reliability** — `uiautomator dump` fails silently during animations or transitions, returns stale XML
- **Element-based interaction** — ADB only supports pixel coordinates; no way to tap "the Submit button" by ID or label
- **Wait conditions** — no way to wait for a specific element to appear; must rely on fixed sleep delays
- **Scroll-to-element** — must manually calculate swipe coordinates rather than asking the framework to scroll an element into view

**Appium would solve these by providing:**
- `driver.get_screenshot_as_png()` — reliable screenshot capture
- `driver.page_source` — always-fresh UI hierarchy in a single call
- `WebDriverWait` — wait for elements to appear/disappear with timeout
- Element finding by ID, XPath, accessibility label — not just pixel coordinates
- element.send_keys()` — proper text input with encoding handled
- Cross-platform — same API works for iOS via XCUITest driver

**Appium cost:**
- Node.js runtime + `npm install -g appium` + `appium driver install uiautomator2`
- Appium server must be running alongside the crawl
- Python client: `pip install Appium-Python-Client`
- Adds ~10 seconds startup time per session
- More complex debugging when things go wrong (two systems to troubleshoot)

**Recommendation:** Start with raw ADB through Phase 1 and Phase 2. If Phase 2 testing reveals recurring problems with text input, flaky UI dumps, or unreliable element detection, introduce Appium as an optional backend in Phase 3. If iOS support becomes a goal (Phase 5), Appium becomes effectively required — there's no raw equivalent to ADB on iOS.

The device interface (`device.py`) is already structured in a way that makes swapping the backend straightforward — all device interaction goes through `Device` methods, so adding an `AppiumDevice` implementation alongside the existing `AdbDevice` would be a contained change.

### Optional: Other Tools

| Tool | What it could add | When to consider |
|---|---|---|
| **scrcpy** | Real-time screen mirroring — watch the crawler live | Phase 2 (debugging aid, not a code dependency) |
| **Maestro** | Declarative YAML-based UI automation — could pre-script login sequences before handing off to the AI crawler | Phase 5 (login/auth handling) |
| **frida** | Runtime instrumentation — intercept API calls to document what data each screen loads | Beyond current scope |

---

## Environment Requirements

To start Phase 2, you need:

1. **Android Studio** (or standalone `sdkmanager` + `emulator`) with at least one AVD configured
2. **ADB** on `$PATH` — comes with Android Studio or platform-tools
3. **`ANTHROPIC_API_KEY`** environment variable set
4. **Python 3.12+** with the project installed (`pip install -e .`)

Quick emulator check:
```bash
adb devices          # Should list your emulator
adb shell wm size    # Should return display dimensions
```
