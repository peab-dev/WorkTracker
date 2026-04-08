#!/usr/bin/env python3
"""rhythm_heatmap.py — Weekly activity heatmap for WorkTracker"""

import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

SUMMARY_DIR = Path.home() / "WorkTracker" / "summaries" / "daily"
HEALTHY_START = 9   # desired work start
HEALTHY_END = 22    # desired work end

# Apps that represent inactive/lock-screen state — not real activity
INACTIVE_APPS = {"loginwindow"}

# ANSI colors
RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
GRAY = "\033[90m"
WHITE = "\033[97m"
BG_GREEN = "\033[42m"
BG_RED = "\033[41m"
BG_YELLOW = "\033[43m"


def get_active_hours(filepath):
    """Extract active hours from the Timeline section of a daily summary.

    Skips rows where the app is an inactive/lock-screen app (e.g. loginwindow).
    """
    hours = set()
    in_timeline = False
    try:
        with open(filepath) as f:
            for line in f:
                if "## Timeline" in line:
                    in_timeline = True
                    continue
                if in_timeline and line.startswith("##"):
                    break
                if not in_timeline:
                    continue
                # Match timeline rows: | From | To | Duration | App | ...
                m = re.match(r'\|\s*(\d{2}):(\d{2})\s*\|\s*(\d{2}):(\d{2})\s*\|', line)
                if m:
                    # Check app name (4th column) — skip inactive apps
                    cols = [c.strip() for c in line.split("|")]
                    # cols: ['', 'HH:MM', 'HH:MM', 'duration', 'app', ...]
                    app_name = cols[4] if len(cols) > 4 else ""
                    if app_name in INACTIVE_APPS:
                        continue

                    start_h = int(m.group(1))
                    end_h = int(m.group(3))
                    # Add all hours in this range
                    if start_h <= end_h:
                        for h in range(start_h, end_h + 1):
                            hours.add(h)
                    else:
                        # Midnight crossing
                        for h in range(start_h, 24):
                            hours.add(h)
                        for h in range(0, end_h + 1):
                            hours.add(h)
    except FileNotFoundError:
        pass
    return hours


def render_week(weeks=1):
    today = datetime.now()
    days = weeks * 7

    # Header
    print()
    print(f"  {CYAN}{BOLD}RHYTHM HEATMAP{RST}  {GRAY}{DIM}— last {days} days{RST}")
    print()

    # Hour header
    print(f"{'':>14}", end="")
    for h in range(24):
        if h % 3 == 0:
            print(f"{GRAY}{h:>3}{RST}", end="")
        else:
            print(f"{DIM}{GRAY}{h:>3}{RST}", end="")
    print()
    print(f"  {GRAY}{'─' * 86}{RST}")

    # Stats accumulators
    total_active = 0
    total_healthy = 0
    total_unhealthy = 0
    days_with_data = 0

    # Days (oldest first)
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        ds = day.strftime("%Y-%m-%d")
        filepath = SUMMARY_DIR / f"{ds}.md"
        hours = get_active_hours(filepath)

        # Day label
        is_today = (i == 0)
        weekday = day.strftime("%a")
        date_str = day.strftime("%d.%m")

        if is_today:
            label = f"{WHITE}{BOLD}{weekday} {date_str}{RST}"
            pad = 14 - len(f"{weekday} {date_str}")
        elif day.weekday() >= 5:  # weekend
            label = f"{YELLOW}{weekday} {date_str}{RST}"
            pad = 14 - len(f"{weekday} {date_str}")
        else:
            label = f"{GRAY}{weekday} {date_str}{RST}"
            pad = 14 - len(f"{weekday} {date_str}")

        print(f"{' ' * pad}{label}", end="")

        day_active = 0
        day_healthy = 0
        day_unhealthy = 0

        for h in range(24):
            if h in hours:
                day_active += 1
                if HEALTHY_START <= h < HEALTHY_END:
                    print(f"  {GREEN}█{RST}", end="")
                    day_healthy += 1
                else:
                    print(f"  {RED}▓{RST}", end="")
                    day_unhealthy += 1
            else:
                if HEALTHY_START <= h < HEALTHY_END:
                    print(f"  {DIM}{GRAY}·{RST}", end="")
                else:
                    print("   ", end="")

        # Day summary
        if hours:
            days_with_data += 1
            total_active += day_active
            total_healthy += day_healthy
            total_unhealthy += day_unhealthy
            pct = (day_healthy / day_active * 100) if day_active else 0
            print(f"  {DIM}{day_active}h{RST}", end="")

        print()

        # Separator between weeks
        if day.weekday() == 6 and i > 0:
            print(f"  {GRAY}{DIM}{'─' * 86}{RST}")

    # Footer
    print(f"  {GRAY}{'─' * 86}{RST}")
    print(f"  {GREEN}█{RST} Active (good hours)  "
          f"{RED}▓{RST} Active (too early/late)  "
          f"{DIM}{GRAY}·{RST} Missed core time")
    print()

    # Weekly stats
    if days_with_data > 0:
        avg_active = total_active / days_with_data
        healthy_pct = (total_healthy / total_active * 100) if total_active else 0
        print(f"  {WHITE}{BOLD}Stats:{RST}  "
              f"{CYAN}{avg_active:.1f}h{RST} avg/day  "
              f"{GREEN}{healthy_pct:.0f}%{RST} in healthy range  "
              f"{RED}{total_unhealthy}h{RST} outside range  "
              f"{GRAY}({days_with_data}/{days} days tracked){RST}")
        print()


def main():
    weeks = 1
    if len(sys.argv) > 1:
        try:
            weeks = int(sys.argv[1])
        except ValueError:
            pass
    render_week(weeks)


if __name__ == "__main__":
    main()
