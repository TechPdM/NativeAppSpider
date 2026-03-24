# AppSpider Development Plan

## Current Status

Phase 1 code fixes, unit tests, and first successful crawl are complete. The tool has been validated against the Android Settings app (10 screens, 17 actions, 13 transitions). Issues found during first crawl (transition recording, screen size override, duplicate names) have been fixed. Next step: additional crawls against more complex apps, then Phase 2 hardening.

| Component | Status | Notes |
|---|---|---|
| Project structure | Done | `pyproject.toml`, package layout, CLI entry point, dev dependencies |
| Architecture docs | Done | `ARCHITECTURE.md`, `CLAUDE.md`, `README.md` with setup guide |
| CLI (`cli.py`) | Done | Startup validation, `--model`/`--verbose` flags, report input validation |
| Device interface (`device.py`) | Done | `ADBError`, return code checking, `is_connected()`, `get_screen_size()` with override support, `is_package_installed()`, `am start` launcher with monkey fallback |
| Perceptual hasher (`hasher.py`) | Done | Average hash + similarity check |
| Screen analyzer (`analyzer.py`) | Done | API key validation, retry with backoff, response validation, default model `claude-sonnet-4-6` |
| Crawl loop (`crawler.py`) | Done | Per-step error recovery, device dimensions for swipes, forward transition recording, duplicate screen name deduplication |
| Reporter (`reporter.py`) | Done | HTML generation with Mermaid, self-contained output |
| Tests | Done | 71 unit tests, all passing (~1.8s), fully mocked |
| Android environment | Done | Homebrew CLI setup, AVD `appspider_test` (Android 14, arm64) |
| First crawl | Done | Settings app: 10 screens, 13 transitions, valid report |

---

## Phase 1: Fix & First Crawl

**Goal:** Fix known bugs, add minimum error handling, and complete a real crawl against an Android emulator.

**Bug fixes:**
- [x] Fix `screenshot()` — removed duplicate ADB call, reads into memory via `io.BytesIO`, validates image size
- [x] Add return code checking in `_run()` — raises `ADBError` on non-zero exit, timeout, or missing ADB
- [x] Detect device dimensions via `adb shell wm size` — cached, used for swipe coordinates

**Validation & error handling:**
- [x] Check device connectivity before crawl starts (`adb devices`)
- [x] Check `ANTHROPIC_API_KEY` is set at CLI startup
- [x] Verify target app package exists and launched
- [x] Wrap crawl loop steps in try/except — log errors per-step, don't crash the whole crawl
- [x] Add API retry with exponential backoff for rate limits and timeouts
- [x] Validate AI response structure — handle missing fields, default missing x/y to fallback

**First real crawls:**
- [x] Set up Android emulator, document setup steps — Homebrew CLI tools, AVD `appspider_test`
- [x] Crawl a simple app (Settings) — 10 screens, 17 actions, 13 transitions
- [x] Crawl a medium app (Clock) — 15 screens, 26 actions, 19 transitions. Navigated all 5 tabs + bedtime setup flow.
- [x] Crawl a complex app (Contacts) — 15 screens, 39 actions, 20 transitions. Onboarding skip, nav drawer, settings, contact form, dialer.
- [ ] Tune hash threshold and settle delay based on real results
- [ ] Review and improve AI prompts based on real Claude responses

**Issues found and fixed during first crawl:**
- [x] Forward tap transitions not recorded — moved edge recording to start of next iteration
- [x] Screen size used physical (320x640) instead of override (1080x1920) — prefer override line
- [x] Duplicate screen names in output — added deduplication with numeric suffixes
- [x] `monkey` launcher returns non-zero exit code — switched to `am start`, monkey as fallback
- [x] Model ID needed to be `claude-sonnet-4-6` (no date suffix) — fixed default, added `--model` CLI flag
- [x] Infinite loop on persistent screenshot failures (Chrome crawl) — added max consecutive failure limit (10), breaks crawl cleanly

**Unit tests** (71 tests, all passing in ~1.8s, fully mocked):
- [x] `test_hasher.py` — hash consistency, similarity detection, threshold boundaries (8 tests)
- [x] `test_device.py` — mock subprocess, XML parsing, error raising, connectivity, screenshots (21 tests)
- [x] `test_analyzer.py` — JSON parsing, fence stripping, malformed response fallback, null handling (18 tests)
- [x] `test_crawler.py` — loop termination, backtracking, graph edges, error recovery (10 tests)
- [x] `test_reporter.py` — HTML generation, screenshot embedding, missing file handling (6 tests)

### Phase 1 Validation Checklist

Results from first crawl (Settings app):
- [x] Crawl completes without crashing
- [x] Discovered screens have meaningful names (not "unknown" or "parse_error")
- [x] Screenshot files are valid PNGs
- [x] State graph has edges (13 transitions recorded)
- [x] HTML report opens in browser and displays all screens
- [x] No infinite loops (10 screens in 17 actions)

---

## Phase 2: Harden

**Goal:** Replay tests, edge case handling, robustness.

**Replay integration tests:**
- [ ] Capture fixture data from Phase 1 crawls (screenshots, UI dumps, API responses)
- [ ] Build `MockDevice` and `MockAnalyzer` that replay fixture sequences
- [ ] Run `Crawler` against mocks — assert graph structure, output files, screen count
- [ ] This enables refactoring without a live device

**Edge cases:**
- [ ] Handle system dialogs (permission prompts, "app not responding", keyboard)
- [ ] Handle orientation changes mid-crawl
- [ ] Detect and scroll through scrollable containers for off-screen elements
- [ ] Improve loop detection — track per-element tap history, not just screen revisit count
- [ ] Add crawl resume — save state to disk periodically, allow resuming after crash

**E2E smoke test:**
- [ ] `test_live_crawl.py` — crawl Settings with `max_screens=5`, assert >1 screen found
- [ ] Marked `@pytest.mark.e2e`, excluded from default test runs

---

## Phase 3: Extend

Future work, not planned in detail. Pursue after Phases 1-2 are solid.

- **Better output:** annotated screenshots, interactive HTML report, Markdown export, JSON schema, diff reports between crawl runs
- **iOS support:** `xcrun simctl` or Appium XCUITest backend, abstract `Device` into protocol
- **Smarter crawling:** cheaper model for navigation / expensive model for analysis, parallel emulator instances, login/auth support, deep link exploration

---

## Dependencies

### Required

| Dependency | What it provides | Install |
|---|---|---|
| **ADB** | All device communication | Android SDK Platform Tools or Android Studio |
| **Android Emulator or device** | Target to crawl | Android Studio AVD Manager |
| **Anthropic API key** | Claude vision API | `export ANTHROPIC_API_KEY=...` |
| **Python 3.12+** | Runtime | System or pyenv |

### Appium (optional, deferred)

Raw ADB is sufficient for coordinate-based tapping and screenshots. It gets fragile for text input (special characters), UI hierarchy reliability (fails during animations), and element-based interaction (no find-by-ID). Appium solves all of these and adds iOS support, but requires a Node.js server running alongside the crawl.

**Decision:** Start with raw ADB. If Phase 1 testing reveals recurring ADB reliability issues, introduce Appium as an optional backend. The `Device` class is already structured so an `AppiumDevice` could slot in alongside the existing ADB implementation. If iOS becomes a goal (Phase 3), Appium is effectively required.

---

## Testing Approach

### Structure

```
tests/
├── conftest.py              # Shared fixtures (mock device, fake screenshots, sample XML)
├── fixtures/                # Real data captured during Phase 1 crawls
├── unit/                    # Fast, fully mocked — run constantly
├── integration/             # Replay tests with fixture data — run before commits
└── e2e/                     # Live device + API — run manually
```

### What each test file covers

| File | Mocking approach | Key areas |
|---|---|---|
| `test_hasher.py` | None (pure logic) | Hash consistency, similarity, thresholds, edge cases |
| `test_device.py` | Mock `subprocess.run` | ADB error handling, XML parsing, bounds parsing, screenshot validation |
| `test_analyzer.py` | Mock `anthropic.Anthropic` | JSON parsing, markdown fence stripping, malformed response fallback, retry logic |
| `test_crawler.py` | Mock `Device` + `Analyzer` | Loop termination, backtracking, graph edges, output serialization |
| `test_reporter.py` | Fixture files on disk | HTML generation, screenshot embedding, missing file handling |
| `test_crawl_replay.py` | `MockDevice` + `MockAnalyzer` from fixtures | Full pipeline against recorded data |

### Running

```bash
pytest tests/unit/ -v           # Fast, no deps — run constantly
pytest tests/integration/ -v    # Replay tests — run before commits
pytest -m e2e                   # Live device — manual sanity checks
```

---

## Environment Setup

Installed via Homebrew CLI approach (see `README.md` for full instructions):

- **Android SDK**: `/opt/homebrew/share/android-commandlinetools`
- **ADB**: `/opt/homebrew/share/android-commandlinetools/platform-tools/adb`
- **AVD**: `appspider_test` (Android 14, Google APIs, arm64-v8a)
- **Java**: Temurin JDK 25
- **Emulator launch**: `emulator -avd appspider_test -no-audio -no-window`
- **Resolution override**: `adb shell wm size 1080x1920`

```bash
export PATH="/opt/homebrew/share/android-commandlinetools/platform-tools:$PATH"
export ANTHROPIC_API_KEY=your-key
adb devices          # Should list your emulator
adb shell wm size    # Should return override dimensions
```
