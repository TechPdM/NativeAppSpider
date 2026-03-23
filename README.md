# AppSpider

Automated mobile app UI spider — crawls and documents screens, elements, and navigation flows.

## Quick Start

```bash
# Install
pip install -e .

# Crawl an app (requires Android emulator with ADB)
appspider crawl com.example.app

# Generate report from existing crawl
appspider report output/com.example.app_20240101_120000/
```

## Requirements

- Python 3.12+
- Android emulator or device connected via ADB
- `ANTHROPIC_API_KEY` environment variable set
