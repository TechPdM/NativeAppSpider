<p align="center">
  <img src="assets/AppSpiderBannerComp.png" width="600" alt="NativeAppSpider banner">
</p>

# NativeAppSpider

Automated mobile app UI spider — crawls and documents screens, elements, and navigation flows. Works on any installed app, no source code access required.

## Quick Start

```bash
# Install
pip install -e .

# Crawl an app (requires Android emulator with ADB)
export ANTHROPIC_API_KEY=your-key
nativeappspider crawl com.example.app

# Generate report from existing crawl
nativeappspider report output/com.example.app_20240101_120000/
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
3. Launch the emulator (DNS works automatically when launched with a GUI)
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

# Launch the emulator (headless, with DNS configured)
emulator -avd appspider_test -no-audio -no-window -dns-server 8.8.8.8,8.8.4.4
```

**Important:** The `-dns-server` flag is required for apps that load network content (maps, API data, etc.). Without it, the headless emulator has IP connectivity but no DNS resolution, so network requests will fail silently.

If you need a specific screen resolution (e.g. for accurate tap coordinates):
```bash
adb shell wm size 1080x1920
adb shell wm density 420
```

To set GPS location (e.g. Bristol, UK — useful for location-based apps):
```bash
adb emu geo fix -2.5879 51.4545
```

### Verify Setup

```bash
adb devices              # Should list your emulator (e.g. "emulator-5554  device")
adb shell wm size        # Should return display dimensions
adb shell ping google.com  # Should resolve and get responses (verifies DNS)
```

## Usage

```bash
# Basic crawl
nativeappspider crawl com.example.app

# Limit scope
nativeappspider crawl com.example.app --max-screens 20 --max-actions 50

# Target a specific device
nativeappspider crawl com.example.app --serial emulator-5554

# Fresh start (clears app data so it launches from the initial screen)
nativeappspider crawl com.example.app --fresh

# Skip specific flows (e.g. avoid registration/login during exploration)
nativeappspider crawl com.example.app --avoid registration --avoid login --avoid "sign up"

# Navigate to a specific screen first, then explore from there
nativeappspider crawl com.example.app --focus map

# Combine focus and avoid for targeted crawling
nativeappspider crawl com.example.app --focus settings --avoid login --avoid registration

# Verbose logging
nativeappspider -v crawl com.example.app

# Regenerate report from previous crawl data
nativeappspider report output/com.example.app_20240101_120000/
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
