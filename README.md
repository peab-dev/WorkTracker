# WORK:TRACKER

Automatic activity tracker for macOS — captures what you work on every 10 seconds, aggregates it into sessions, and generates daily/weekly/monthly summaries with AI-powered analysis.

Built for macOS (Apple Silicon), designed as a fully local, private productivity tool.

![Version](https://img.shields.io/badge/version-0.0.1-brightgreen)
![Status](https://img.shields.io/badge/status-public%20beta-blue)
![Platform](https://img.shields.io/badge/platform-macOS-lightgrey)
![License](https://img.shields.io/badge/license-open%20source-green)

`Runs Locally` · `Open Source` · `No Blackbox` · `App Tracking` · `Flow Insights`

[Got To Full Documentation][https://peab.at/WorkTracker/docs/index.html]

---

## Quick Start

### 1. Install

```bash
git clone https://github.com/peab-dev/WorkTracker.git
cd WorkTracker
./install.sh
```

### 2. Grant Permissions

Grant permissions in **macOS Settings → Privacy & Security → Accessibility + Screen Recording** to Terminal.

### 3. Run

```bash
source ~/.zshrc   # or: wtrl
wt status
```

---

## Features

### Core
- macOS app + activity tracking
- Sessions, intensity, and reports
- Daily / weekly / monthly aggregation

### Local-First
- Runs entirely on your Mac
- No black box, no fees
- Your data stays local

### Optional: AI Power Up
Level up your Work:Tracker output. Uncover unseen activity patterns & time-wastings with the power of AI. Connect local LLMs of your choice & improve your workflow.

---

## Useful Commands

| Command      | Description                          |
|--------------|--------------------------------------|
| `wt status`  | Show service status and latest data  |
| `wt daily`   | Run daily aggregation now            |
| `wt web`     | Start the local web dashboard        |

---

## Made in Austria

WorkTracker v0.0.1 — made with <3 by [peab.at](https://peab.at)
