# AppSpider

A proof-of-concept tool that automatically crawls mobile app UIs and documents screens, elements, and navigation flows.

## Architecture

- **Device layer**: Android emulator controlled via ADB (screenshots + UI hierarchy XML)
- **Crawler engine**: State-graph explorer — each unique screen is a node, each action is an edge. Uses perceptual hashing to detect already-visited screens.
- **AI layer**: Claude Computer Use API (beta) to analyze screenshots, identify interactive elements, and decide navigation actions
- **Output**: Per-screen documentation (screenshot + description + element inventory), state transition graph (Mermaid/JSON), replayable flow definitions

## Tech Stack

- Python 3.12+
- `anthropic` SDK (Computer Use beta)
- `pure-python-adb` or subprocess ADB for device control
- `Pillow` for image processing and perceptual hashing
- `networkx` for state graph management
- `uv` for dependency management

## Project Structure

```
src/appspider/
  __init__.py
  cli.py          # CLI entry point
  crawler.py      # Main crawl loop and state graph
  device.py       # ADB device interface (screenshots, taps, swipes, UI tree)
  analyzer.py     # Claude Computer Use integration for screen analysis
  hasher.py       # Perceptual hashing for screen deduplication
  reporter.py     # Output generation (HTML report, Mermaid diagrams, JSON)
```

## Commands

```bash
uv run appspider crawl <package-name>   # Crawl an app
uv run appspider report <crawl-dir>     # Generate report from crawl data
```

## Dev Commands

```bash
uv sync                  # Install dependencies
uv run pytest            # Run tests
uv run ruff check src/   # Lint
```

## Key Design Decisions

- ADB over Appium for the PoC — fewer dependencies, simpler setup
- Perceptual hashing (average hash) over pixel-exact comparison — handles minor rendering differences
- Breadth-first exploration with backtracking via back button
- Claude analyzes each screen to identify tappable elements and suggest which to explore next
- Cost control: configurable max screens and max depth limits
