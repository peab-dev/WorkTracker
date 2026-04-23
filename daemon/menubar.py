#!/usr/bin/env python3
"""WorkTracker Menubar Widget — macOS Status Bar Integration

Shows live productivity stats in the macOS menu bar.
Polls /api/live every 30 seconds for real-time data.

Usage:
    python3 menubar.py          # Standalone
    wt menubar                  # Via CLI

Requires: pyobjc-framework-Cocoa (already in WorkTracker deps)
"""

import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSApp,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSFont,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSTimer,
    NSRunLoop,
    NSDefaultRunLoopMode,
    NSAttributedString,
    NSMutableAttributedString,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSColor,
    NSDictionary,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSTextTab,
    NSTextAlignmentRight,
    NSTextAlignmentCenter,
    NSView,
    NSBezierPath,
    NSGradient,
)
from Foundation import NSObject, NSLog, NSMakeRange, NSMakeRect, NSMakePoint, NSMakeSize

# ── Config ──────────────────────────────────────────────────────────────
API_URL = "http://127.0.0.1:7880/api/live"
POLL_INTERVAL = 30.0  # seconds
RETRY_INTERVAL = 60.0  # seconds after failure

# Base dir only used to locate config.yaml — data paths come from config.
_BASE_DIR = Path.home() / "WorkTracker"
_CONFIG_PATH = _BASE_DIR / "daemon" / "config.yaml"


def _paths_from_config() -> tuple[Path, Path]:
    """Resolve (sessions_dir, summaries_daily_dir) from config.yaml.

    Falls back to the canonical ~/WorkTracker layout when the config is
    missing or malformed, so the widget stays usable during first-run /
    broken-config scenarios.
    """
    try:
        import yaml  # local import — avoids startup cost if never called twice
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        sessions = Path(cfg["aggregator"]["sessions_dir"]).expanduser()
        summaries = Path(cfg["aggregator"]["summaries_dir"]).expanduser()
        return sessions, summaries / "daily"
    except Exception:
        return (
            _BASE_DIR / "data" / "sessions",
            _BASE_DIR / "summaries" / "daily",
        )


SESSIONS_DIR, SUMMARIES_DAILY_DIR = _paths_from_config()

# Unicode blocks for progress bars
BAR_FULL = "█"
BAR_EMPTY = "░"
SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def fmt_duration(sec):
    """Format seconds as Xh Ym."""
    if sec is None or sec == 0:
        return "0m"
    h, m = divmod(int(sec), 3600)
    _, m = divmod(int(sec) % 3600, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def fmt_rate(val):
    """Format a rate value."""
    if val is None:
        return "–"
    return str(int(val))


def fmt_uptime(seconds):
    """Format seconds as 'Xd Yh' / 'Xh Ym' / 'Xm Ys'."""
    if seconds is None or seconds <= 0:
        return "—"
    s = int(seconds)
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d >= 1:
        return f"{d}d {h}h"
    if h >= 1:
        return f"{h}h {m:02d}m"
    if m >= 1:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def intensity_bar(pct, width=10):
    """Create a mini bar from percentage (0..100)."""
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct / 100 * width))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def intensity_color(pct):
    """Return a semantic color name for an intensity percentage."""
    if pct >= 50:
        return "green"
    if pct >= 20:
        return "cyan"
    if pct >= 8:
        return "yellow"
    return "orange"


def load_today_sessions():
    """Load today's sessions JSON, or empty list on any error."""
    path = SESSIONS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"
    try:
        with open(path) as f:
            return json.load(f) or []
    except Exception:
        return []


def aggregate_topics(sessions, top_n=8, min_sec=60):
    """Aggregate sessions by topic, return sorted list of {name, sec, project, count}."""
    totals = {}
    projects = {}
    counts = {}
    for s in sessions:
        topic = (s.get("topic") or "").strip()
        if not topic:
            continue
        dur = int(s.get("duration_seconds", 0) or 0)
        totals[topic] = totals.get(topic, 0) + dur
        counts[topic] = counts.get(topic, 0) + 1
        # Keep the project of the longest session for this topic
        if totals[topic] == dur or dur > totals.get(projects.get(topic, ""), 0):
            projects[topic] = s.get("project", "")
    items = [
        {
            "name": t,
            "sec": totals[t],
            "project": projects.get(t, ""),
            "count": counts[t],
        }
        for t in totals
        if totals[t] >= min_sec
    ]
    items.sort(key=lambda x: x["sec"], reverse=True)
    return items[:top_n]


# Named colors → NSColor factory method name. Module-level so PyObjC
# doesn't try to interpret it as an Objective-C selector.
_COLOR_MAP = {
    "red": "systemRedColor",
    "green": "systemGreenColor",
    "orange": "systemOrangeColor",
    "yellow": "systemYellowColor",
    "cyan": "systemTealColor",
    "blue": "systemBlueColor",
    "purple": "systemPurpleColor",
    "pink": "systemPinkColor",
}


def mb_color(name):
    """Map a semantic color name to an NSColor instance."""
    if not name:
        return NSColor.labelColor()
    fn = _COLOR_MAP.get(name)
    if fn:
        return getattr(NSColor, fn)()
    return NSColor.labelColor()


# ── Rhythm data helpers (10:00 → 10:00 day definition) ─────────────────
# A "rhythm day" starts at 10:00 on its start date and runs to 10:00 the
# next calendar day. That means each rhythm row spans two markdown files.

_INACTIVE_APPS = {"loginwindow"}
DAY_START_HOUR = 10  # 10:00 is the rhythm-day boundary


def _read_active_hours_for_date(date_obj):
    """Parse the daily markdown timeline and return a set of active hours."""
    ds = date_obj.strftime("%Y-%m-%d")
    filepath = SUMMARIES_DAILY_DIR / f"{ds}.md"
    hours = set()
    try:
        in_timeline = False
        with open(filepath) as f:
            for line in f:
                if "## Timeline" in line:
                    in_timeline = True
                    continue
                if in_timeline and line.startswith("##"):
                    break
                if not in_timeline:
                    continue
                m = re.match(r'\|\s*(\d{2}):(\d{2})\s*\|\s*(\d{2}):(\d{2})\s*\|', line)
                if m:
                    cols = [c.strip() for c in line.split("|")]
                    app_name = cols[4] if len(cols) > 4 else ""
                    if app_name in _INACTIVE_APPS:
                        continue
                    sh, eh = int(m.group(1)), int(m.group(3))
                    if sh <= eh:
                        for hh in range(sh, eh + 1):
                            hours.add(hh)
                    else:
                        for hh in range(sh, 24):
                            hours.add(hh)
                        for hh in range(0, eh + 1):
                            hours.add(hh)
    except FileNotFoundError:
        pass
    return hours


def rhythm_day_active_hours(start_date):
    """Return the set of active hours for a 10:00→10:00 rhythm day.

    The span is:
      * start_date 10:00 … 23:59 → from ``start_date``'s report
      * (start_date + 1) 00:00 … 09:59 → from next day's report

    Returned hours are in 0..23 (actual wall-clock). Use ``DAY_START_HOUR``
    to map them to display order.
    """
    hours = set()
    # Hours 10..23 from start_date
    for h in _read_active_hours_for_date(start_date):
        if h >= DAY_START_HOUR:
            hours.add(h)
    # Hours 0..9 from next day
    next_day = start_date + timedelta(days=1)
    for h in _read_active_hours_for_date(next_day):
        if h < DAY_START_HOUR:
            hours.add(h)
    return hours


def rotate_hourly_to_day_start(hourly):
    """Rotate a 24-bucket hourly array so that index 0 is ``DAY_START_HOUR``.

    Returns (rotated_data, display_hours) where display_hours is the list of
    actual wall-clock hours in the order they appear in rotated_data.
    """
    if not hourly or len(hourly) != 24:
        return list(hourly or []), list(range(24))
    rotated = hourly[DAY_START_HOUR:] + hourly[:DAY_START_HOUR]
    display_hours = list(range(DAY_START_HOUR, 24)) + list(range(0, DAY_START_HOUR))
    return rotated, display_hours


def recent_topics(sessions, n=5):
    """Last N sessions with a topic, newest-first."""
    out = []
    for s in reversed(sessions):
        topic = (s.get("topic") or "").strip()
        if not topic:
            continue
        out.append(
            {
                "name": topic,
                "project": s.get("project", ""),
                "sec": int(s.get("duration_seconds", 0) or 0),
                "app": s.get("app_name", ""),
            }
        )
        if len(out) >= n:
            break
    return out


# ── Custom NSView for the hourly activity line chart ────────────────────
# The only real graphic in the menu — everything else is text-laid-out
# NSAttributedString which is more reliable inside NSMenu.


class WTLineChartView(NSView):
    """Filled-area line chart for hourly activity (24-hour buckets)."""

    _data = None
    _max_val = 1.0
    _accent = None
    _now_hour = None
    _display_hours = None  # list of wall-clock hours, one per data bucket

    def initWithFrame_data_accent_hours_(self, frame, data, accent, hours):
        self = objc.super(WTLineChartView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._data = list(data or [])
        self._max_val = max(self._data) if self._data else 1.0
        if self._max_val <= 0:
            self._max_val = 1.0
        self._accent = accent or NSColor.systemTealColor()
        self._now_hour = datetime.now().hour
        # hours[i] == wall-clock hour at data index i (for labeling the x-axis)
        if hours and len(hours) == len(self._data):
            self._display_hours = list(hours)
        else:
            self._display_hours = list(range(len(self._data)))
        return self

    def drawRect_(self, rect):
        bounds = self.bounds()
        w = float(bounds.size.width)
        h = float(bounds.size.height)
        pad_left = 10.0
        pad_right = 10.0
        pad_top = 6.0
        pad_bottom = 14.0  # leave room for hour labels

        chart_w = w - pad_left - pad_right
        chart_h = h - pad_top - pad_bottom

        data = self._data or [0] * 24
        if len(data) != 24:
            return
        n = len(data)

        # Background rounded rect
        bg_rect = NSMakeRect(pad_left - 4, pad_bottom - 2, chart_w + 8, chart_h + 6)
        bg_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bg_rect, 6, 6)
        NSColor.colorWithSRGBRed_green_blue_alpha_(1, 1, 1, 0.05).setFill()
        bg_path.fill()

        # Build the polyline points (one per hour)
        step = chart_w / (n - 1) if n > 1 else chart_w
        points = []
        for i, v in enumerate(data):
            x = pad_left + i * step
            y = pad_bottom + (v / self._max_val) * chart_h
            points.append((x, y))

        # Filled area
        if points:
            fill_path = NSBezierPath.bezierPath()
            fill_path.moveToPoint_(NSMakePoint(points[0][0], pad_bottom))
            for x, y in points:
                fill_path.lineToPoint_(NSMakePoint(x, y))
            fill_path.lineToPoint_(NSMakePoint(points[-1][0], pad_bottom))
            fill_path.closePath()

            # Gradient fill for the area
            c_top = self._accent.colorWithAlphaComponent_(0.55)
            c_bot = self._accent.colorWithAlphaComponent_(0.05)
            grad = NSGradient.alloc().initWithStartingColor_endingColor_(c_top, c_bot)
            grad.drawInBezierPath_angle_(fill_path, 270.0)

            # Line on top
            line_path = NSBezierPath.bezierPath()
            line_path.setLineWidth_(1.5)
            line_path.moveToPoint_(NSMakePoint(*points[0]))
            for p in points[1:]:
                line_path.lineToPoint_(NSMakePoint(*p))
            self._accent.setStroke()
            line_path.stroke()

        # Baseline
        base = NSBezierPath.bezierPath()
        base.moveToPoint_(NSMakePoint(pad_left, pad_bottom))
        base.lineToPoint_(NSMakePoint(pad_left + chart_w, pad_bottom))
        base.setLineWidth_(0.5)
        NSColor.separatorColor().setStroke()
        base.stroke()

        # "Now" indicator — map the current wall-clock hour to its position
        # in the rotated display array.
        if self._now_hour in self._display_hours:
            now_idx = self._display_hours.index(self._now_hour)
            if 0 <= now_idx < n:
                nx = pad_left + now_idx * step
                dot_size = 4.0
                dot = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(nx - dot_size / 2, pad_bottom - 1, dot_size, dot_size)
                )
                NSColor.systemRedColor().setFill()
                dot.fill()

        # Hour labels — use the wall-clock hour at each position.
        # Pick positions every 4 buckets so the labels don't crowd each other.
        lbl_font = NSFont.systemFontOfSize_(8.5)
        lbl_attrs = {
            NSFontAttributeName: lbl_font,
            NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
        }
        label_positions = [0, 4, 8, 12, 16, 20, n - 1]
        seen = set()
        for idx in label_positions:
            if idx < 0 or idx >= n or idx in seen:
                continue
            seen.add(idx)
            hr = self._display_hours[idx] if idx < len(self._display_hours) else idx
            lx = pad_left + idx * step
            s = NSAttributedString.alloc().initWithString_attributes_(
                f"{hr:02d}", lbl_attrs
            )
            sw = s.size().width
            s.drawAtPoint_(NSMakePoint(lx - sw / 2, 0))


class WTGaugeRowView(NSView):
    """Three circular ring gauges side-by-side, styled like the CPU-Stats app.

    Each gauge is a ring-shaped progress indicator with a value label and a
    small caption below. Center gauge is larger (hero stat).
    """

    _gauges = None  # list of (label, value_text, pct 0..1, accent NSColor)

    def initWithFrame_gauges_(self, frame, gauges):
        self = objc.super(WTGaugeRowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._gauges = list(gauges or [])
        return self

    def drawRect_(self, rect):
        if not self._gauges:
            return
        bounds = self.bounds()
        w = float(bounds.size.width)
        h = float(bounds.size.height)
        n = len(self._gauges)

        # Layout: three columns, center is hero (larger radius).
        col_w = w / n
        big_radius = 26.0
        small_radius = 18.0
        ring_width = 4.5
        track_alpha = 0.18

        for i, (label, value_text, pct, accent) in enumerate(self._gauges):
            is_hero = (i == n // 2) and n >= 3
            r = big_radius if is_hero else small_radius
            cx = col_w * i + col_w / 2.0
            cy = h / 2.0 + 4  # leave room for label under gauge

            # Background ring (track)
            track = NSBezierPath.bezierPath()
            track.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
                NSMakePoint(cx, cy), r, 0, 360
            )
            track.setLineWidth_(ring_width)
            NSColor.colorWithSRGBRed_green_blue_alpha_(
                1, 1, 1, track_alpha
            ).setStroke()
            track.stroke()

            # Foreground progress arc: starts at top (90°), clockwise
            pct_clamped = max(0.0, min(1.0, float(pct or 0.0)))
            if pct_clamped > 0:
                start_angle = 90.0
                end_angle = 90.0 - pct_clamped * 360.0
                arc = NSBezierPath.bezierPath()
                arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                    NSMakePoint(cx, cy), r, start_angle, end_angle, True
                )
                arc.setLineWidth_(ring_width)
                arc.setLineCapStyle_(1)  # round cap
                (accent or NSColor.systemTealColor()).setStroke()
                arc.stroke()

            # Value text in center of ring
            val_font_size = 13.0 if is_hero else 11.0
            val_attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_weight_(val_font_size, 0.5),
                NSForegroundColorAttributeName: NSColor.labelColor(),
            }
            val_str = NSAttributedString.alloc().initWithString_attributes_(
                str(value_text), val_attrs
            )
            val_size = val_str.size()
            val_str.drawAtPoint_(
                NSMakePoint(cx - val_size.width / 2.0, cy - val_size.height / 2.0)
            )

            # Caption below the gauge
            cap_attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_(9.5),
                NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
            }
            cap_str = NSAttributedString.alloc().initWithString_attributes_(
                str(label), cap_attrs
            )
            cap_size = cap_str.size()
            cap_str.drawAtPoint_(
                NSMakePoint(cx - cap_size.width / 2.0, cy - r - ring_width - 12)
            )


class WTBarRowView(NSView):
    """One row with: colored dot · label · progress bar · value · percent.

    Replaces the ASCII-Unicode bars in the Projects / Topics sections with
    real NSBezierPath rendering for a cleaner look.
    """

    _dot = None
    _label = ""
    _value = ""
    _pct = 0.0  # 0..1
    _accent = None
    _suffix = ""  # optional small muted text at end (e.g. project name)

    def initWithFrame_dot_label_value_pct_accent_suffix_(
        self, frame, dot_color, label, value, pct, accent_color, suffix
    ):
        self = objc.super(WTBarRowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._dot = dot_color
        self._label = str(label)
        self._value = str(value)
        self._pct = max(0.0, min(1.0, float(pct or 0.0)))
        self._accent = accent_color or NSColor.systemTealColor()
        self._suffix = str(suffix or "")
        return self

    def drawRect_(self, rect):
        bounds = self.bounds()
        w = float(bounds.size.width)
        h = float(bounds.size.height)

        pad = 14.0
        dot_size = 7.0
        label_x = pad + dot_size + 6
        label_w = 110.0
        value_w = 58.0
        suffix_w = 90.0

        # Bar sits between label (right edge) and value (left edge)
        bar_x = label_x + label_w + 6
        bar_right_x = w - pad - value_w - (suffix_w if self._suffix else 0) - 6
        bar_w = max(20.0, bar_right_x - bar_x)
        bar_h = 6.0
        bar_y = h / 2.0 - bar_h / 2.0

        # Colored dot
        if self._dot:
            dot_rect = NSMakeRect(pad, h / 2.0 - dot_size / 2.0, dot_size, dot_size)
            dot_path = NSBezierPath.bezierPathWithOvalInRect_(dot_rect)
            self._dot.setFill()
            dot_path.fill()

        # Label
        lbl_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(12.0),
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }
        lbl_str = NSAttributedString.alloc().initWithString_attributes_(
            self._label, lbl_attrs
        )
        lbl_size = lbl_str.size()
        lbl_str.drawInRect_(
            NSMakeRect(label_x, h / 2.0 - lbl_size.height / 2.0, label_w, lbl_size.height)
        )

        # Bar track
        track_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bar_x, bar_y, bar_w, bar_h), bar_h / 2.0, bar_h / 2.0
        )
        NSColor.colorWithSRGBRed_green_blue_alpha_(1, 1, 1, 0.12).setFill()
        track_path.fill()

        # Bar fill
        fill_w = bar_w * self._pct
        if fill_w > 1:
            fill_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(bar_x, bar_y, fill_w, bar_h), bar_h / 2.0, bar_h / 2.0
            )
            self._accent.setFill()
            fill_path.fill()

        # Value text (right-aligned within value_w)
        val_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(11.5, 0.4),
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }
        val_str = NSAttributedString.alloc().initWithString_attributes_(
            self._value, val_attrs
        )
        val_size = val_str.size()
        val_x = w - pad - val_size.width - (suffix_w if self._suffix else 0)
        val_str.drawAtPoint_(NSMakePoint(val_x, h / 2.0 - val_size.height / 2.0))

        # Optional suffix (e.g. project name) in cyan, small
        if self._suffix:
            suf_attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_(10.5),
                NSForegroundColorAttributeName: NSColor.systemTealColor(),
            }
            suf_str = NSAttributedString.alloc().initWithString_attributes_(
                "· " + self._suffix, suf_attrs
            )
            suf_size = suf_str.size()
            suf_x = w - pad - suf_size.width
            suf_str.drawAtPoint_(NSMakePoint(suf_x, h / 2.0 - suf_size.height / 2.0))


class WTHeroView(NSView):
    """Big hero number + label pair, rendered like a dashboard headline."""

    _value = ""
    _label = ""
    _accent = None
    _sub = ""

    def initWithFrame_value_label_accent_sub_(self, frame, value, label, accent, sub):
        self = objc.super(WTHeroView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._value = str(value)
        self._label = str(label)
        self._accent = accent or NSColor.systemTealColor()
        self._sub = str(sub or "")
        return self

    def drawRect_(self, rect):
        bounds = self.bounds()
        w = float(bounds.size.width)
        h = float(bounds.size.height)

        # Big value
        val_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(26.0, 0.6),
            NSForegroundColorAttributeName: self._accent,
        }
        val_str = NSAttributedString.alloc().initWithString_attributes_(
            self._value, val_attrs
        )
        val_size = val_str.size()
        val_x = (w - val_size.width) / 2.0
        val_y = h - val_size.height - 4
        val_str.drawAtPoint_(NSMakePoint(val_x, val_y))

        # Small caption under the value
        cap_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(10.5, 0.3),
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
        }
        cap_str = NSAttributedString.alloc().initWithString_attributes_(
            self._label.upper(), cap_attrs
        )
        cap_size = cap_str.size()
        cap_str.drawAtPoint_(
            NSMakePoint((w - cap_size.width) / 2.0, val_y - cap_size.height - 1)
        )

        # Optional sub-line (e.g. percentage of target)
        if self._sub:
            sub_attrs = {
                NSFontAttributeName: NSFont.systemFontOfSize_(10.5),
                NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
            }
            sub_str = NSAttributedString.alloc().initWithString_attributes_(
                self._sub, sub_attrs
            )
            sub_size = sub_str.size()
            sub_str.drawAtPoint_(NSMakePoint((w - sub_size.width) / 2.0, 0))


class WTRhythmView(NSView):
    """7-day × 24h rhythm heatmap, day boundary at 10:00.

    Rows: 7 rhythm days, bottom row = today (highlighted).
    Columns: 24 hours starting at 10:00, ending at 09:00 next day.
    Each cell is a rounded square; color encodes active / healthy / weekend.
    """

    _rows = None  # list of {label, active:set[int], is_today:bool, is_weekend:bool, total:int}
    _hours_order = None
    _label_hours = None  # indices in _hours_order to draw a label for

    def initWithFrame_rows_(self, frame, rows):
        self = objc.super(WTRhythmView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._rows = list(rows or [])
        self._hours_order = (
            list(range(DAY_START_HOUR, 24)) + list(range(0, DAY_START_HOUR))
        )
        # Label every 3 columns
        self._label_hours = list(range(0, 24, 3))
        return self

    def drawRect_(self, rect):
        if not self._rows:
            return
        bounds = self.bounds()
        w = float(bounds.size.width)
        h = float(bounds.size.height)

        pad_left = 12.0
        pad_right = 12.0
        pad_top = 20.0  # hour header row
        pad_bottom = 22.0  # legend row
        label_w = 38.0
        count_w = 32.0

        rows_n = len(self._rows)
        row_area_h = h - pad_top - pad_bottom
        row_h = row_area_h / rows_n if rows_n else 0

        chart_x = pad_left + label_w
        chart_w = w - chart_x - count_w - pad_right
        cell_w = chart_w / 24.0
        cell_size = min(cell_w * 0.78, row_h * 0.72, 12.0)

        # Hour header labels (10, 13, 16, 19, 22, 1, 4, 7)
        hdr_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(8.5, 0.2),
            NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
        }
        for idx in self._label_hours:
            hr = self._hours_order[idx]
            x = chart_x + idx * cell_w + cell_w / 2
            lbl = NSAttributedString.alloc().initWithString_attributes_(
                f"{hr:02d}", hdr_attrs
            )
            sz = lbl.size()
            lbl.drawAtPoint_(NSMakePoint(x - sz.width / 2, h - pad_top + 4))

        # Rows (top→bottom visually = oldest→today)
        for ri, row in enumerate(self._rows):
            # ri=0 is oldest; rhythm rows stack top-to-bottom
            top_y = h - pad_top - (ri + 1) * row_h
            cy = top_y + row_h / 2

            is_today = bool(row.get("is_today"))
            is_weekend = bool(row.get("is_weekend"))
            active = set(row.get("active") or set())
            total = int(row.get("total", 0))

            # Today highlight pill
            if is_today:
                hl_rect = NSMakeRect(
                    pad_left - 2, top_y - 1,
                    w - pad_left - pad_right + 4, row_h + 2
                )
                hl_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    hl_rect, 5, 5
                )
                NSColor.colorWithSRGBRed_green_blue_alpha_(1, 1, 1, 0.07).setFill()
                hl_path.fill()

            # Weekday label
            lbl_color = NSColor.labelColor()
            if is_today:
                lbl_color = NSColor.systemGreenColor()
            elif is_weekend:
                lbl_color = NSColor.systemOrangeColor()
            else:
                lbl_color = NSColor.secondaryLabelColor()

            lbl_font = NSFont.systemFontOfSize_weight_(
                10.5, 0.55 if is_today else 0.25
            )
            lbl_attrs = {
                NSFontAttributeName: lbl_font,
                NSForegroundColorAttributeName: lbl_color,
            }
            lbl = NSAttributedString.alloc().initWithString_attributes_(
                str(row.get("label", "")), lbl_attrs
            )
            lbl_sz = lbl.size()
            lbl.drawAtPoint_(NSMakePoint(pad_left, cy - lbl_sz.height / 2))

            # Cells
            for col, hr in enumerate(self._hours_order):
                x = chart_x + col * cell_w + (cell_w - cell_size) / 2
                cell_rect = NSMakeRect(x, cy - cell_size / 2, cell_size, cell_size)
                path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    cell_rect, 2.5, 2.5
                )

                # Inactive track
                if hr not in active:
                    NSColor.colorWithSRGBRed_green_blue_alpha_(
                        1, 1, 1, 0.07
                    ).setFill()
                    path.fill()
                    continue

                # Active cell — color based on healthy hour range and weekday
                healthy = 10 <= hr < 22
                if is_today:
                    base = NSColor.systemGreenColor() if healthy else NSColor.systemPurpleColor()
                    alpha = 1.0 if healthy else 0.85
                elif is_weekend:
                    base = NSColor.systemOrangeColor() if healthy else NSColor.systemPurpleColor()
                    alpha = 0.90 if healthy else 0.70
                else:
                    base = NSColor.systemTealColor() if healthy else NSColor.systemPurpleColor()
                    alpha = 0.80 if healthy else 0.60

                base.colorWithAlphaComponent_(alpha).setFill()
                path.fill()

            # Right-side total count
            if total > 0:
                cnt_color = NSColor.labelColor()
                if is_today:
                    cnt_color = NSColor.systemGreenColor()
                elif total < 4:
                    cnt_color = NSColor.tertiaryLabelColor()
                elif is_weekend:
                    cnt_color = NSColor.systemOrangeColor()

                cnt_font = NSFont.systemFontOfSize_weight_(
                    10.5, 0.55 if is_today else 0.25
                )
                cnt_attrs = {
                    NSFontAttributeName: cnt_font,
                    NSForegroundColorAttributeName: cnt_color,
                }
                cnt_str = NSAttributedString.alloc().initWithString_attributes_(
                    f"{total}h", cnt_attrs
                )
                c_sz = cnt_str.size()
                cnt_str.drawAtPoint_(
                    NSMakePoint(w - pad_right - c_sz.width, cy - c_sz.height / 2)
                )

        # Legend at the bottom
        leg_y = 2.0
        leg_pad = pad_left
        leg_attrs_lbl = {
            NSFontAttributeName: NSFont.systemFontOfSize_(9.0),
            NSForegroundColorAttributeName: NSColor.tertiaryLabelColor(),
        }
        leg_specs = [
            (NSColor.systemTealColor(), "Tag"),
            (NSColor.systemOrangeColor(), "Weekend"),
            (NSColor.systemPurpleColor(), "spät/früh"),
        ]
        lx = leg_pad
        for color, label in leg_specs:
            dot_rect = NSMakeRect(lx, leg_y + 3, 7, 7)
            dot_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                dot_rect, 1.5, 1.5
            )
            color.colorWithAlphaComponent_(0.85).setFill()
            dot_path.fill()
            lx += 11
            lbl = NSAttributedString.alloc().initWithString_attributes_(
                label, leg_attrs_lbl
            )
            lbl.drawAtPoint_(NSMakePoint(lx, leg_y + 1))
            lx += lbl.size().width + 12


class WTRecentRowView(NSView):
    """A 'Zuletzt' entry: accent dot · topic · project pill."""

    _label = ""
    _project = ""
    _accent = None

    def initWithFrame_label_project_accent_(self, frame, label, project, accent):
        self = objc.super(WTRecentRowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._label = str(label)
        self._project = str(project or "")
        self._accent = accent or NSColor.systemTealColor()
        return self

    def drawRect_(self, rect):
        bounds = self.bounds()
        w = float(bounds.size.width)
        h = float(bounds.size.height)
        pad = 14.0
        dot_size = 6.0

        cy = h / 2.0

        # Dot
        dot_rect = NSMakeRect(pad, cy - dot_size / 2, dot_size, dot_size)
        dot_path = NSBezierPath.bezierPathWithOvalInRect_(dot_rect)
        self._accent.setFill()
        dot_path.fill()

        # Topic label
        lbl_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(12.0),
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }
        lbl = NSAttributedString.alloc().initWithString_attributes_(
            self._label, lbl_attrs
        )
        lbl_size = lbl.size()
        label_x = pad + dot_size + 8
        lbl.drawAtPoint_(NSMakePoint(label_x, cy - lbl_size.height / 2))

        # Project pill (right-aligned)
        if self._project:
            pill_font = NSFont.systemFontOfSize_weight_(10.5, 0.4)
            pill_attrs = {
                NSFontAttributeName: pill_font,
                NSForegroundColorAttributeName: NSColor.systemTealColor(),
            }
            pill_str = NSAttributedString.alloc().initWithString_attributes_(
                self._project, pill_attrs
            )
            p_sz = pill_str.size()
            pill_pad_x = 7.0
            pill_pad_y = 2.0
            pill_w = p_sz.width + pill_pad_x * 2
            pill_h = p_sz.height + pill_pad_y * 2
            pill_x = w - pad - pill_w
            pill_y = cy - pill_h / 2
            pill_rect = NSMakeRect(pill_x, pill_y, pill_w, pill_h)
            pill_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                pill_rect, pill_h / 2, pill_h / 2
            )
            NSColor.systemTealColor().colorWithAlphaComponent_(0.14).setFill()
            pill_path.fill()
            NSColor.systemTealColor().colorWithAlphaComponent_(0.35).setStroke()
            pill_path.setLineWidth_(0.75)
            pill_path.stroke()
            pill_str.drawAtPoint_(
                NSMakePoint(pill_x + pill_pad_x, pill_y + pill_pad_y)
            )


class WTServiceRowView(NSView):
    """A service status row: status dot · name · status pill (right)."""

    _name = ""
    _status = ""
    _ok = True

    def initWithFrame_name_status_ok_(self, frame, name, status, ok):
        self = objc.super(WTServiceRowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._name = str(name)
        self._status = str(status)
        self._ok = bool(ok)
        return self

    def drawRect_(self, rect):
        bounds = self.bounds()
        w = float(bounds.size.width)
        h = float(bounds.size.height)
        pad = 14.0
        cy = h / 2.0

        accent = NSColor.systemGreenColor() if self._ok else NSColor.systemRedColor()

        # Soft halo behind the dot — gives a subtle "pulse" look
        halo_size = 14.0
        halo_rect = NSMakeRect(
            pad - halo_size / 2 + 4, cy - halo_size / 2, halo_size, halo_size
        )
        halo_path = NSBezierPath.bezierPathWithOvalInRect_(halo_rect)
        accent.colorWithAlphaComponent_(0.18).setFill()
        halo_path.fill()

        # Status dot
        dot_size = 8.0
        dot_rect = NSMakeRect(
            pad + 4 - dot_size / 2 + (halo_size - dot_size) / 2,
            cy - dot_size / 2, dot_size, dot_size
        )
        dot_path = NSBezierPath.bezierPathWithOvalInRect_(dot_rect)
        accent.setFill()
        dot_path.fill()

        # Service name
        name_attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(12.5, 0.4),
            NSForegroundColorAttributeName: NSColor.labelColor(),
        }
        name_str = NSAttributedString.alloc().initWithString_attributes_(
            self._name, name_attrs
        )
        name_sz = name_str.size()
        name_x = pad + halo_size + 8
        name_str.drawAtPoint_(NSMakePoint(name_x, cy - name_sz.height / 2))

        # Status pill (right-aligned) with accent background
        pill_font = NSFont.systemFontOfSize_weight_(11.0, 0.5)
        pill_attrs = {
            NSFontAttributeName: pill_font,
            NSForegroundColorAttributeName: accent,
        }
        pill_str = NSAttributedString.alloc().initWithString_attributes_(
            self._status, pill_attrs
        )
        p_sz = pill_str.size()
        pill_pad_x = 9.0
        pill_pad_y = 3.0
        pill_w = p_sz.width + pill_pad_x * 2
        pill_h = p_sz.height + pill_pad_y * 2
        pill_x = w - pad - pill_w
        pill_y = cy - pill_h / 2
        pill_rect = NSMakeRect(pill_x, pill_y, pill_w, pill_h)
        pill_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            pill_rect, pill_h / 2, pill_h / 2
        )
        accent.colorWithAlphaComponent_(0.16).setFill()
        pill_path.fill()
        accent.colorWithAlphaComponent_(0.45).setStroke()
        pill_path.setLineWidth_(0.75)
        pill_path.stroke()
        pill_str.drawAtPoint_(
            NSMakePoint(pill_x + pill_pad_x, pill_y + pill_pad_y)
        )


class WorkTrackerMenubar(NSObject):
    """macOS Menubar widget for WorkTracker."""

    statusItem = None
    menu = None
    timer = None
    last_data = None
    api_ok = False

    def applicationDidFinishLaunching_(self, notification):
        """Set up status bar item and menu."""
        # Hide dock icon
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        # Create status bar item
        self.statusItem = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.statusItem.setHighlightMode_(True)

        # Initial title
        self.statusItem.setTitle_("WT ⏳")

        # Build menu
        self.menu = NSMenu.alloc().init()
        self.menu.setAutoenablesItems_(False)
        self.statusItem.setMenu_(self.menu)

        # Initial fetch
        self.refresh_(None)

        # Set up timer for periodic refresh
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_INTERVAL, self, "refresh:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSDefaultRunLoopMode)

    def refresh_(self, timer):
        """Fetch data from API and update menu."""
        threading.Thread(target=self._fetch_and_update, daemon=True).start()

    def _fetch_and_update(self):
        """Background fetch, then update UI on main thread."""
        try:
            req = urllib.request.Request(API_URL, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.last_data = data
            self.api_ok = True
        except Exception as e:
            self.api_ok = False
            self.last_data = None

        # Update UI on main thread
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "updateUI:", None, False
        )

    def updateUI_(self, _):
        """Update menubar title and dropdown menu (must run on main thread)."""
        if not self.api_ok or not self.last_data:
            self.statusItem.setTitle_("WT ⚠")
            self._build_error_menu()
            return

        data = self.last_data
        day = data.get("day")
        live = data.get("live")

        # ── Menubar title ───────────────────────────────────────
        if day:
            active = fmt_duration(day.get("total_sec", 0))
            switches_ph = day.get("switches_ph", 0)
            title = f"WT {active} · {int(switches_ph)}sw/h"
        else:
            title = "WT 0m"

        # Add current keys/min if actively typing
        if live and live.get("keys_pm", 0) > 0:
            title += f" · ⌨{live['keys_pm']}"

        self.statusItem.setTitle_(title)

        # ── Dropdown menu ───────────────────────────────────────
        self.menu.removeAllItems()

        # Load today's sessions for topic data
        sessions = load_today_sessions()

        # Header
        self._add_header("WorkTracker Live")
        self._add_separator()

        # Current activity
        if live:
            app_name = live.get("app", "–")
            window = live.get("window", "")
            if window and len(window) > 42:
                window = window[:39] + "…"

            # App + window on one line if space, else two lines
            self._add_item(f"  ▸ {app_name}", enabled=False, bold=True)
            if window:
                self._add_item(f"    {window}", enabled=False)

            # Media
            media = live.get("media")
            if media and media.get("title"):
                artist = media.get("artist", "")
                track = media.get("title", "")
                if len(track) > 35:
                    track = track[:33] + "…"
                if artist:
                    self._add_item(f"  ♫ {artist} — {track}", enabled=False, color="cyan")
                else:
                    self._add_item(f"  ♫ {track}", enabled=False, color="cyan")

            # Input rates — color based on activity level
            keys = live.get("keys_pm", 0)
            clicks = live.get("clicks_pm", 0)
            idle_kb = live.get("idle_kb", 0)

            act_color = "green" if keys >= 60 or clicks >= 40 else (
                "cyan" if keys >= 20 or clicks >= 10 else None
            )
            self._add_item(
                f"  ⌨ {keys:>3}/min   🖱 {clicks:>3}/min   💤 {idle_kb}s idle",
                enabled=False,
                color=act_color,
            )

            # Git
            git = live.get("git")
            if git and git.get("repo"):
                branch = git.get("branch", "–")
                self._add_item(
                    f"  ⎇ {git['repo']} → {branch}", enabled=False, color="purple"
                )

            self._add_separator()

        # Day stats
        if day:
            total_sec_val = day.get("total_sec", 0)
            total = fmt_duration(total_sec_val)
            sessions_count = day.get("sessions", 0)
            focus = day.get("focus_count", 0)
            focus_t = fmt_duration(day.get("focus_sec", 0))
            focus_sec_val = day.get("focus_sec", 0)
            sw = day.get("switches", 0)
            sw_ph = day.get("switches_ph", 0)
            keys = day.get("keys", 0)
            clicks = day.get("clicks", 0)

            # ── Hero: 3 ring gauges at the top ──────────────────
            # Left: Sessions count (progress vs. soft target 20)
            # Center (hero): Arbeitszeit in % of 8h target
            # Right: Focus count (progress vs. target 4)
            target_sec = 8 * 3600
            pct_work = min(1.0, total_sec_val / target_sec) if target_sec else 0
            pct_focus = min(1.0, focus / 4.0)
            pct_sessions = min(1.0, sessions_count / 20.0)

            work_accent = intensity_color(pct_work * 100)
            focus_accent = "green" if focus >= 2 else "orange"

            self._add_gauge_row(
                [
                    ("Sessions", str(sessions_count), pct_sessions, "cyan"),
                    ("Arbeitszeit", total, pct_work, work_accent),
                    ("Focus", str(focus), pct_focus, focus_accent),
                ],
                height=92.0,
            )

            # ── Nutzungsverlauf (Hourly Line Chart) ─────────────
            hourly = day.get("hourly", [])
            if hourly and any(h > 0 for h in hourly):
                self._add_header("Nutzungsverlauf · ab 10:00")
                rotated, display_hours = rotate_hourly_to_day_start(hourly)
                self._add_line_chart(
                    rotated,
                    accent_color_name="cyan",
                    height=70.0,
                    display_hours=display_hours,
                )

            # ── Heute — detail stat rows ────────────────────────
            self._add_header("Heute")

            focus_pct = (focus_sec_val / total_sec_val * 100) if total_sec_val else 0
            self._add_stat_row(
                "Focus-Anteil",
                f"{fmt_duration(focus_sec_val)}  ({focus_pct:.0f}%)",
                dot_color="green" if focus > 0 else "orange",
                value_color="green" if focus > 0 else "orange",
            )

            sw_color = "green" if sw_ph < 20 else "yellow" if sw_ph < 40 else "orange"
            self._add_stat_row(
                "App-Wechsel", f"{sw}  ({int(sw_ph)}/h)",
                dot_color=sw_color, value_color=sw_color,
            )
            self._add_stat_row("Keystrokes", f"{keys:,}", dot_color="purple")
            self._add_stat_row("Clicks", f"{clicks:,}", dot_color="blue")

            # ── Projects with real NSView bar rows ──────────────
            projects = day.get("projects", [])
            if projects:
                self._add_header("Projekte")
                for p in projects[:6]:
                    name = p.get("name", "?")
                    if len(name) > 18:
                        name = name[:17] + "…"
                    sec = p.get("sec", 0)
                    pct = p.get("pct", 0) / 100.0
                    bar_color = intensity_color(p.get("pct", 0))
                    self._add_bar_row(
                        label=name,
                        value=fmt_duration(sec),
                        pct=pct,
                        accent_color_name=bar_color,
                        dot_color_name=bar_color,
                        suffix="",
                    )

            # ── Topics with real bar rows ───────────────────────
            topics = aggregate_topics(sessions, top_n=6, min_sec=60)
            if topics:
                self._add_header("Themen")
                max_sec = max((t["sec"] for t in topics), default=1)
                for t in topics:
                    name = t["name"]
                    if len(name) > 26:
                        name = name[:25] + "…"
                    tpct = (t["sec"] / max_sec) if max_sec else 0
                    bar_color = intensity_color(tpct * 100)
                    proj = t["project"] or ""
                    if proj and len(proj) > 12:
                        proj = proj[:11] + "…"
                    self._add_bar_row(
                        label=name,
                        value=fmt_duration(t["sec"]),
                        pct=tpct,
                        accent_color_name="cyan",
                        dot_color_name=bar_color,
                        suffix=proj,
                    )

            # Recent topics (last 3 sessions with topics)
            recent = recent_topics(sessions, n=3)
            if recent:
                self._add_header("Zuletzt")
                for r in recent:
                    name = r["name"]
                    if len(name) > 32:
                        name = name[:31] + "…"
                    proj = r["project"] or ""
                    if proj and len(proj) > 14:
                        proj = proj[:13] + "…"
                    self._add_recent_row(
                        label=name,
                        project=proj,
                        accent_color_name="cyan",
                    )

        # ── Dienste: Collector with running-since ───────────────
        services = data.get("services", {})
        if services:
            self._add_header("Dienste")
            collector = services.get("collector", {}) or {}
            col_running = bool(collector.get("running")) or (
                collector.get("loaded", False) and collector.get("pid") is not None
            )
            uptime_sec = collector.get("uptime_sec")
            if col_running:
                status_text = (
                    f"läuft · {fmt_uptime(uptime_sec)}" if uptime_sec else "läuft"
                )
                self._add_service_row("Collector", status_text, ok=True)
            else:
                self._add_service_row("Collector", "offline", ok=False)

        self._add_separator()

        # Rhythm heatmap
        self._build_rhythm_section()

        # Actions
        self._add_action("Dashboard öffnen", "openDashboard:")
        self._add_action("Jetzt aktualisieren", "refresh:")
        self._add_separator()
        self._add_action("Beenden", "terminate:")

    def _build_error_menu(self):
        """Build menu when API is unreachable."""
        self.menu.removeAllItems()
        self._add_header("WorkTracker")
        self._add_separator()
        self._add_item("  ⚠ API nicht erreichbar", enabled=False, color="red")
        self._add_item("  localhost:7880 antwortet nicht", enabled=False)
        self._add_item("  → wt web  starten", enabled=False, color="cyan")
        self._add_separator()
        self._add_action("Erneut versuchen", "refresh:")
        self._add_separator()
        self._add_action("Beenden", "terminate:")

    def _build_rhythm_section(self):
        """Add a 7-day rhythm heatmap with a 10:00→10:00 day definition.

        Each rhythm row represents a 24-hour span starting at 10:00 on its
        start date. The 'current' rhythm day is the one that contains now:
          * if now.hour >= 10 → starts at today 10:00
          * if now.hour <  10 → starts at yesterday 10:00
        """
        self._add_header("Rhythmus · 7 Tage · 10 → 10")

        now = datetime.now()
        # Determine start date of the rhythm day containing 'now'.
        if now.hour >= DAY_START_HOUR:
            current_day_start = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            current_day_start = (now - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        rows = []
        # Oldest first, so the newest (today) row appears at the bottom.
        for i in range(6, -1, -1):
            start = current_day_start - timedelta(days=i)
            active = rhythm_day_active_hours(start)
            # Weekday label reflects the start date of the rhythm day
            weekday = start.strftime("%a")
            rows.append({
                "label": weekday,
                "active": active,
                "total": len(active),
                "is_today": (i == 0),
                "is_weekend": start.weekday() >= 5,
            })

        self._add_rhythm(rows, height=170.0)
        self._add_separator()

    # ── Menu helpers ────────────────────────────────────────────
    # Menu item target width — used to place right-aligned tab stops.
    MENU_WIDTH = 300.0

    def _add_header(self, text):
        """Stats-app style centered section title (replaces dim gray headers).

        Looks like:   ———————  Details  ———————
        The divider lines are drawn by padding with en-dashes so we stay
        pure-text (works reliably inside NSMenu without custom views).
        """
        # Clean the text of any leading arrows/prefix from earlier versions.
        txt = text.replace("▸", "").strip()

        # Surround with light dashes for the divider look.
        pretty = f"———  {txt}  ———"

        pstyle = NSMutableParagraphStyle.alloc().init()
        pstyle.setAlignment_(NSTextAlignmentCenter)

        attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(11.0, 0.4),
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
            NSParagraphStyleAttributeName: pstyle,
        }
        attr_str = NSAttributedString.alloc().initWithString_attributes_(pretty, attrs)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        item.setAttributedTitle_(attr_str)
        item.setEnabled_(False)
        self.menu.addItem_(item)

    def _base_font(self):
        return NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.0)

    def _stat_paragraph_style(self):
        """Paragraph style with a right-aligned tab stop at the menu width.

        Used by _add_stat_row so values line up on the right edge the same
        way the CPU-stats screenshot does.
        """
        pstyle = NSMutableParagraphStyle.alloc().init()
        tab = NSTextTab.alloc().initWithTextAlignment_location_options_(
            NSTextAlignmentRight, self.MENU_WIDTH, NSDictionary.dictionary()
        )
        pstyle.setTabStops_([tab])
        return pstyle

    def _add_stat_row(self, label, value, dot_color=None, value_color=None):
        """Add a 'Label: [dot] ............ Value' row (Stats-app style).

        The *dot_color* draws a small coloured square next to the label (like
        the System / Benutzer / Leerlauf rows in the screenshot). *value* is
        rendered in bold so it pops out visually.
        """
        pstyle = self._stat_paragraph_style()
        base_font = NSFont.systemFontOfSize_(12.5)
        bold_font = NSFont.systemFontOfSize_weight_(12.5, 0.55)

        # Build: "  ■ Label:\tValue"
        # Use two spaces indent, then square marker, label, colon, tab, value.
        marker = "■ " if dot_color else "  "
        raw = f"  {marker}{label}:\t{value}"
        mas = NSMutableAttributedString.alloc().init()
        attrs_base = {
            NSFontAttributeName: base_font,
            NSForegroundColorAttributeName: NSColor.labelColor(),
            NSParagraphStyleAttributeName: pstyle,
        }
        mas.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(raw, attrs_base)
        )

        # Colour the marker square if requested.
        if dot_color and "■" in raw:
            idx = raw.index("■")
            mas.addAttribute_value_range_(
                NSForegroundColorAttributeName,
                mb_color(dot_color),
                NSMakeRange(idx, 1),
            )

        # Bold the value (everything after the tab).
        if "\t" in raw:
            val_start = raw.index("\t") + 1
            val_len = len(raw) - val_start
            mas.addAttribute_value_range_(
                NSFontAttributeName, bold_font, NSMakeRange(val_start, val_len)
            )
            if value_color:
                mas.addAttribute_value_range_(
                    NSForegroundColorAttributeName,
                    mb_color(value_color),
                    NSMakeRange(val_start, val_len),
                )

        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        item.setAttributedTitle_(mas)
        item.setEnabled_(False)
        self.menu.addItem_(item)

    def _add_line_chart(self, hourly_data, accent_color_name="cyan",
                         height=68.0, display_hours=None):
        """Embed a filled-area line chart as a custom-view menu item.

        ``display_hours`` is an optional list of wall-clock hours matching
        ``hourly_data`` one-to-one. If omitted, labels default to [0..N-1].
        """
        try:
            accent = mb_color(accent_color_name)
            frame = NSMakeRect(0, 0, self.MENU_WIDTH + 20, height)
            hours = list(display_hours) if display_hours else list(range(len(hourly_data or [])))
            view = WTLineChartView.alloc().initWithFrame_data_accent_hours_(
                frame, hourly_data, accent, hours
            )
            if view is None:
                return
            item = NSMenuItem.alloc().init()
            item.setView_(view)
            self.menu.addItem_(item)
        except Exception:
            pass

    def _add_gauge_row(self, gauges, height=86.0):
        """Add a row of ring gauges.

        *gauges* is a list of (label, value_text, pct, accent_color_name).
        """
        try:
            specs = [
                (label, value_text, pct, mb_color(accent))
                for (label, value_text, pct, accent) in gauges
            ]
            frame = NSMakeRect(0, 0, self.MENU_WIDTH + 20, height)
            view = WTGaugeRowView.alloc().initWithFrame_gauges_(frame, specs)
            if view is None:
                return
            item = NSMenuItem.alloc().init()
            item.setView_(view)
            self.menu.addItem_(item)
        except Exception:
            pass

    def _add_bar_row(self, label, value, pct, accent_color_name="cyan",
                      dot_color_name=None, suffix=""):
        """Add a single progress-bar row (dot · label · bar · value · suffix)."""
        try:
            frame = NSMakeRect(0, 0, self.MENU_WIDTH + 20, 22.0)
            view = WTBarRowView.alloc().initWithFrame_dot_label_value_pct_accent_suffix_(
                frame,
                mb_color(dot_color_name) if dot_color_name else None,
                label, value, pct,
                mb_color(accent_color_name),
                suffix,
            )
            if view is None:
                return
            item = NSMenuItem.alloc().init()
            item.setView_(view)
            self.menu.addItem_(item)
        except Exception:
            pass

    def _add_rhythm(self, rows, height=170.0):
        """Add the 7-day rhythm heatmap as a custom-view menu item."""
        try:
            frame = NSMakeRect(0, 0, self.MENU_WIDTH + 20, height)
            view = WTRhythmView.alloc().initWithFrame_rows_(frame, rows)
            if view is None:
                return
            item = NSMenuItem.alloc().init()
            item.setView_(view)
            self.menu.addItem_(item)
        except Exception:
            pass

    def _add_recent_row(self, label, project, accent_color_name="cyan"):
        """Add a 'Zuletzt' entry (dot · topic · project-pill)."""
        try:
            frame = NSMakeRect(0, 0, self.MENU_WIDTH + 20, 22.0)
            view = WTRecentRowView.alloc().initWithFrame_label_project_accent_(
                frame, label, project, mb_color(accent_color_name)
            )
            if view is None:
                return
            item = NSMenuItem.alloc().init()
            item.setView_(view)
            self.menu.addItem_(item)
        except Exception:
            pass

    def _add_service_row(self, name, status, ok):
        """Add a service status row (halo dot · name · accent pill)."""
        try:
            frame = NSMakeRect(0, 0, self.MENU_WIDTH + 20, 26.0)
            view = WTServiceRowView.alloc().initWithFrame_name_status_ok_(
                frame, name, status, ok
            )
            if view is None:
                return
            item = NSMenuItem.alloc().init()
            item.setView_(view)
            self.menu.addItem_(item)
        except Exception:
            pass

    def _add_hero(self, value, label, accent_color_name="green", sub=""):
        """Add a hero headline with a big value + small caption."""
        try:
            frame = NSMakeRect(0, 0, self.MENU_WIDTH + 20, 60.0)
            view = WTHeroView.alloc().initWithFrame_value_label_accent_sub_(
                frame, value, label, mb_color(accent_color_name), sub
            )
            if view is None:
                return
            item = NSMenuItem.alloc().init()
            item.setView_(view)
            self.menu.addItem_(item)
        except Exception:
            pass

    def _add_item(self, text, enabled=True, color=None, bold=False):
        """Add a regular menu item. No more dim/grey — default label color."""
        if bold:
            font = NSFont.monospacedSystemFontOfSize_weight_(12.0, 0.5)  # medium-bold
        else:
            font = self._base_font()

        attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: mb_color(color),
        }
        attr_str = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        item.setAttributedTitle_(attr_str)
        item.setEnabled_(enabled)
        self.menu.addItem_(item)

    def _add_item_multi(self, segments):
        """Add a menu item built from multiple (text, color) segments.

        Each segment keeps its own color so we can mix a coloured bar with
        default-coloured text on the same row.
        """
        font = self._base_font()
        mas = NSMutableAttributedString.alloc().init()
        for text, color in segments:
            if not text:
                continue
            attrs = {
                NSFontAttributeName: font,
                NSForegroundColorAttributeName: mb_color(color),
            }
            part = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            mas.appendAttributedString_(part)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        item.setAttributedTitle_(mas)
        item.setEnabled_(False)
        self.menu.addItem_(item)

    def _add_separator(self):
        """Add a menu separator."""
        self.menu.addItem_(NSMenuItem.separatorItem())

    def _add_action(self, text, selector):
        """Add a clickable action item."""
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            text, selector, ""
        )
        item.setTarget_(self)
        self.menu.addItem_(item)

    # ── Actions ─────────────────────────────────────────────────

    def openDashboard_(self, sender):
        """Open the web dashboard in default browser."""
        import webbrowser
        webbrowser.open("http://127.0.0.1:7880")

    def terminate_(self, sender):
        """Quit the menubar widget."""
        NSApp.terminate_(self)


def main():
    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda *_: NSApp.terminate_(None))
    signal.signal(signal.SIGTERM, lambda *_: NSApp.terminate_(None))

    app = NSApplication.sharedApplication()
    delegate = WorkTrackerMenubar.alloc().init()
    app.setDelegate_(delegate)
    app.run()


if __name__ == "__main__":
    main()
