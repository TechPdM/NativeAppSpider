# AppSpider Development Plan

## Current Status

All code fixes and unit tests from Phase 1 are complete. The codebase is ready for real-device testing. Next step: set up an Android emulator and run the first crawl.

| Component | Status | Notes |
|---|---|---|
| Project structure | Done | `pyproject.toml`, package layout, CLI entry point |
| Architecture docs | Done | `ARCHITECTURE.md`, `CLAUDE.md` |
| CLI (`cli.py`) | Done | Startup validation (API key, device connectivity, screen size), verbose flag |
| Device interface (`device.py`) | Done | `ADBError` exceptions, return code checking, `is_connected()`, `get_screen_size()`, `is_package_installed()`, in-memory screenshots |
| Perceptual hasher (`hasher.py`) | Done | Average hash + similarity check |
| Screen analyzer (`analyzer.py`) | Done | API key validation, retry with backoff, response structure validation, unknown action fallback |
| Crawl loop (`crawler.py`) | Done | Per-step error recovery, real device dimensions for swipes, app launch verification |
| Reporter (`reporter.py`) | Done | HTML generation with Mermaid, self-contained output |
| Tests | Done | 71 unit tests, all passing (1.8s), fully mocked |

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
- [ ] Set up Android emulator, document setup steps
- [ ] Crawl a simple app (e.g. Settings) — verify full pipeline end-to-end
- [ ] Crawl a medium app (tabs, lists, modals) — stress test the state graph
- [ ] Crawl a complex app (login, scrollable content, nested navigation)
- [ ] Tune hash threshold and settle delay based on real results
- [ ] Review and improve AI prompts based on real Claude responses

**Unit tests** (71 tests, all passing in ~1.8s, fully mocked):
- [x] `test_hasher.py` — hash consistency, similarity detection, threshold boundaries (8 tests)
- [x] `test_device.py` — mock subprocess, XML parsing, error raising, connectivity, screenshots (21 tests)
- [x] `test_analyzer.py` — JSON parsing, fence stripping, malformed response fallback, null handling (18 tests)
- [x] `test_crawler.py` — loop termination, backtracking, graph edges, error recovery (10 tests)
- [x] `test_reporter.py` — HTML generation, screenshot embedding, missing file handling (6 tests)

### Phase 1 Validation Checklist

Run after each real crawl:
- [ ] Crawl completes without crashing
- [ ] Discovered screens have meaningful names (not "unknown" or "parse_error")
- [ ] Screenshot files are valid PNGs
- [ ] State graph has edges (transitions were recorded)
- [ ] HTML report opens in browser and displays all screens
- [ ] No infinite loops (crawl didn't burn all actions on 2-3 screens)

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

To start Phase 1, you need:

1. **Android Studio** (or standalone SDK) with at least one AVD configured
2. **ADB** on `$PATH`
3. **`ANTHROPIC_API_KEY`** set
4. **Python 3.12+** with the project installed (`pip install -e .`)

```bash
adb devices          # Should list your emulator
adb shell wm size    # Should return display dimensions
```
