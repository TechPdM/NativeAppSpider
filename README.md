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

# Auto-dismiss dialogs (consent banners, cookie popups, onboarding overlays)
nativeappspider crawl com.example.app --dismiss consent --dismiss cookie --dismiss onboarding

# Navigate to a specific screen first, then explore from there
nativeappspider crawl com.example.app --focus map

# Combine focus, avoid, and dismiss for targeted crawling
nativeappspider crawl com.example.app --focus settings --avoid login --dismiss consent

# Use a YAML config file instead of CLI flags
nativeappspider crawl --config examples/zapmap.yaml

# Use a cheaper model for navigation decisions to reduce costs
nativeappspider crawl com.example.app --decision-model claude-haiku-4-5

# Use a specific model for both analysis and decisions
nativeappspider crawl com.example.app --model claude-haiku-4-5

# Resume a previous crawl with a higher budget
nativeappspider crawl --continue output/com.example.app_20240101_120000/ --max-screens 30

# Verbose logging
nativeappspider -v crawl com.example.app

# Regenerate report from previous crawl data
nativeappspider report output/com.example.app_20240101_120000/
```

### Config Files

Instead of passing many CLI flags, use a YAML config file:

```yaml
# examples/myapp.yaml
package: com.example.app
max_screens: 10
max_actions: 60
fresh: true
focus: settings menu
scroll_discovery: false
avoid:
  - registration
  - login
dismiss:
  - consent
  - privacy
  - cookie
# Optional: use a cheaper model for navigation decisions
decision_model: claude-haiku-4-5
```

Run with: `nativeappspider crawl --config examples/myapp.yaml`

## Smart Crawl Behaviours

The crawler includes several heuristics to improve efficiency and avoid getting stuck:

- **Force-stop before launch** — the app is force-stopped before each crawl so it always starts from its main activity, even without `--fresh`. App data is preserved.
- **Breadth-first from focus screen** — after reaching the `--focus` target, the crawler systematically tries each untried element on that screen before going deep into any one path.
- **Screen name deduplication** — if Claude identifies a screen with the same name as one already recorded (e.g. a form with different fields focused), it's treated as a revisit instead of consuming a new screen slot.
- **Text field filtering** — text input fields (EditText, TextInputEditText, etc.) are excluded from navigation decisions. They're still documented in the screen analysis, but the crawler won't waste actions tapping into form fields.
- **Toxic screen detection** — screens that repeatedly cause the app to leave (e.g. photo pickers that open a system activity) are automatically skipped after 2 relaunches.
- **Auto-dismiss app dialogs** — screens matching `--dismiss` keywords are dismissed automatically by tapping accept/close buttons, preventing the crawler from getting stuck on consent banners or cookie popups that don't respond to the back button.
- **Ad region masking** — known ad SDK elements are masked in screenshots before perceptual hashing, so rotating ads don't cause the same screen to be treated as new.

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
# Install dependencies
uv sync

# Run tests (71 unit tests, ~2s, no device needed)
uv run pytest tests/unit/ -v

# Lint
uv run ruff check src/
```
