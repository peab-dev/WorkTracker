# WorkTracker

**Automatic activity tracker for macOS** — captures what you work on every 10 seconds, aggregates it into sessions, and generates daily/weekly/monthly summaries with AI-powered analysis.

Built for macOS (Apple Silicon), designed as a fully local, private productivity tool.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  LAYER 1 — DATA COLLECTION (Python daemon, every 10s)   │
│  collector.py → JSONL files (1 file per day)            │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│  LAYER 2 — AGGREGATION (Python + Pandas, via launchd)   │
│  aggregator.py → Session JSON + Markdown export         │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│  LAYER 3 — AI ANALYSIS (Claude Cowork, scheduled tasks) │
│  Reads Markdown → Generates summaries + suggestions     │
└─────────────────────────────────────────────────────────┘
```

## What It Tracks

- **Active application** and window title
- **Project detection** via configurable pattern matching
- **Input activity** (keystroke/mouse click counts — no content)
- **Clipboard content** (optional)
- **Media playback** state
- **All open windows** (optional)
- **Idle detection** with configurable threshold

## Project Structure

```
~/WorkTracker/
├── daemon/
│   ├── collector.py          # Data collection daemon
│   ├── aggregator.py         # Session aggregation
│   ├── config.yaml           # Main configuration
│   ├── project_patterns.yaml # Project detection rules
│   └── requirements.txt
├── launchd/                  # macOS launchd service configs
├── data/
│   ├── snapshots/            # Raw JSONL (1 file per day)
│   └── sessions/             # Aggregated sessions
├── summaries/
│   ├── daily/
│   ├── weekly/
│   └── monthly/
└── logs/
```

## Tech Stack

- **Python 3.11+** — collector & aggregator
- **Pandas** — data aggregation
- **pyobjc** — macOS system APIs (window info, media state)
- **launchd** — daemon scheduling
- **Claude Cowork** — AI-powered analysis layer

## Setup

> ⚠️ This project is in active development. Full setup instructions coming soon.

1. Clone the repo
2. Install dependencies: `pip install -r daemon/requirements.txt`
3. Configure `daemon/config.yaml` and `daemon/project_patterns.yaml`
4. Install launchd services from `launchd/`

## Privacy

All data stays **100% local**. No cloud services, no telemetry, no external APIs (except optional AI analysis via Claude). Your activity data never leaves your machine.

## License

MIT
