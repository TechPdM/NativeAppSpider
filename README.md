# AppSpider

Automated mobile app UI spider — crawls and documents screens, elements, and navigation flows. Works on any installed app, no source code access required.

## Quick Start

```bash
# Install
pip install -e .

# Crawl an app (requires Android emulator with ADB)
export ANTHROPIC_API_KEY=your-key
appspider crawl com.example.app

# Generate report from existing crawl
appspider report output/com.example.app_20240101_120000/
```

## Requirements

- Python 3.12+
- Android emulator or device connected via ADB
- `ANTHROPIC_API_KEY` environment variable set

## Android Setup

You need ADB and an Android emulator. Two options:

### Option A: Android Studio (full IDE, ~3GB)

Download [Android Studio](https://developer.android.com/studio). It includes the emulator, AVD manager, and ADB. After install:

1. Open Android Studio → Tools → Device Manager
2. Create a new virtual device (e.g. Pixel 7, API 34)
3. Launch the emulator
4. ADB is at `~/Library/Android/sdk/platform-tools/adb` — add it to your PATH:
   ```bash
   export PATH="$HOME/Library/Android/sdk/platform-tools:$PATH"
   ```

### Option B: Command-line only (~1GB)

Lighter setup, no IDE needed. Requires [Homebrew](https://brew.sh/):

```bash
# Install Android command-line tools
brew install --cask android-commandlinetools

# Install platform-tools (ADB), emulator, and a system image
sdkmanager "platform-tools" "emulator" "platforms;android-34" \
  "system-images;android-34;google_apis;arm64-v8a"

# Create an emulator (AVD)
avdmanager create avd -n appspider_test \
  -k "system-images;android-34;google_apis;arm64-v8a"

# Launch the emulator
emulator -avd appspider_test
```

### Verify Setup

```bash
adb devices          # Should list your emulator (e.g. "emulator-5554  device")
adb shell wm size    # Should return display dimensions (e.g. "Physical size: 1080x2400")
```

## Usage

```bash
# Basic crawl
appspider crawl com.example.app

# Limit scope
appspider crawl com.example.app --max-screens 20 --max-actions 50

# Target a specific device
appspider crawl com.example.app --serial emulator-5554

# Verbose logging
appspider -v crawl com.example.app

# Regenerate report from previous crawl data
appspider report output/com.example.app_20240101_120000/
```

## Output

Each crawl produces a timestamped directory:

```
output/com.example.app_20240101_120000/
├── screenshots/       # PNG screenshot of each unique screen
├── screens.json       # Screen names, descriptions, elements, activity names
├── transitions.json   # Navigation edges between screens
├── flow.mmd           # Mermaid diagram of the navigation graph
└── report.html        # Self-contained visual report (open in browser)
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests (71 unit tests, ~2s, no device needed)
pytest tests/unit/ -v

# Lint
ruff check src/
```
