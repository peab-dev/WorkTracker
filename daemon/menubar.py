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
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime

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
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSColor,
    NSDictionary,
)
from Foundation import NSObject, NSLog

# ── Config ──────────────────────────────────────────────────────────────
API_URL = "http://127.0.0.1:7880/api/live"
POLL_INTERVAL = 30.0  # seconds
RETRY_INTERVAL = 60.0  # seconds after failure


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


def intensity_bar(pct, width=6):
    """Create a mini bar from percentage."""
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


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
            title = f"WT {active} | {int(switches_ph)}sw/h"
        else:
            title = "WT 0m"

        # Add current keys/min if actively typing
        if live and live.get("keys_pm", 0) > 0:
            title += f" | ⌨{live['keys_pm']}"

        self.statusItem.setTitle_(title)

        # ── Dropdown menu ───────────────────────────────────────
        self.menu.removeAllItems()

        # Header
        self._add_header("WorkTracker Live")
        self._add_separator()

        # Current activity
        if live:
            app_name = live.get("app", "–")
            window = live.get("window", "")
            if window and len(window) > 40:
                window = window[:37] + "..."
            self._add_item(f"  {app_name}", enabled=False)
            if window:
                self._add_item(f"     {window}", enabled=False, dim=True)

            # Media
            media = live.get("media")
            if media and media.get("title"):
                artist = media.get("artist", "")
                track = media.get("title", "")
                if artist:
                    self._add_item(f"  ♫ {artist} — {track}", enabled=False, dim=True)
                else:
                    self._add_item(f"  ♫ {track}", enabled=False, dim=True)

            # Input rates
            keys = live.get("keys_pm", 0)
            clicks = live.get("clicks_pm", 0)
            idle_kb = live.get("idle_kb", 0)
            self._add_item(
                f"  ⌨ {keys}/min   🖱 {clicks}/min   💤 {idle_kb}s idle",
                enabled=False,
            )

            # Git
            git = live.get("git")
            if git and git.get("repo"):
                branch = git.get("branch", "–")
                self._add_item(f"  ⎇ {git['repo']} → {branch}", enabled=False, dim=True)

            self._add_separator()

        # Day stats
        if day:
            self._add_header("Heute")
            total = fmt_duration(day.get("total_sec", 0))
            sessions = day.get("sessions", 0)
            focus = day.get("focus_count", 0)
            focus_t = fmt_duration(day.get("focus_sec", 0))
            sw = day.get("switches", 0)
            sw_ph = day.get("switches_ph", 0)
            keys = day.get("keys", 0)
            clicks = day.get("clicks", 0)

            self._add_item(f"  Arbeitszeit:     {total}", enabled=False)
            self._add_item(f"  Sessions:          {sessions}", enabled=False)

            # Focus - color-coded
            focus_str = f"  Focus:               {focus} ({focus_t})"
            if focus == 0:
                self._add_item(focus_str, enabled=False, color="red")
            else:
                self._add_item(focus_str, enabled=False, color="green")

            # Switches - color-coded
            sw_color = "green" if sw_ph < 20 else "orange" if sw_ph < 40 else "red"
            self._add_item(
                f"  App-Wechsel:    {sw} ({int(sw_ph)}/h)", enabled=False, color=sw_color
            )
            self._add_item(f"  Keystrokes:      {keys:,}", enabled=False)
            self._add_item(f"  Clicks:              {clicks:,}", enabled=False)

            self._add_separator()

            # Projects
            projects = day.get("projects", [])
            if projects:
                self._add_header("Projekte")
                for p in projects[:6]:
                    name = p.get("name", "?")
                    sec = p.get("sec", 0)
                    pct = p.get("pct", 0)
                    bar = intensity_bar(pct)
                    time_str = fmt_duration(sec)
                    self._add_item(
                        f"  {bar}  {name} ({time_str}, {pct:.0f}%)", enabled=False
                    )
                self._add_separator()

            # Hourly sparkline
            hourly = day.get("hourly", [])
            if hourly and any(h > 0 for h in hourly):
                self._add_header("Stunden")
                spark_chars = " ▁▂▃▄▅▆▇█"
                max_h = max(hourly) if max(hourly) > 0 else 1
                spark = ""
                labels = ""
                for i, h in enumerate(hourly):
                    if h > 0 or (i > 0 and hourly[i - 1] > 0) or (i < 23 and hourly[i + 1] > 0 if i < 23 else False):
                        idx = min(int(h / max_h * 8), 8)
                        spark += spark_chars[idx]
                        labels += f"{i % 10}"
                if spark.strip():
                    # Find first and last active hour for label
                    active_hours = [i for i, h in enumerate(hourly) if h > 0]
                    if active_hours:
                        start_h = active_hours[0]
                        end_h = active_hours[-1]
                        self._add_item(f"  {spark}  ({start_h}:00–{end_h + 1}:00)", enabled=False)
                self._add_separator()

        # Services status
        services = data.get("services", {})
        if services:
            collector = services.get("collector", {})
            col_ok = collector.get("loaded", False) and collector.get("pid") is not None
            dot = "🟢" if col_ok else "🔴"
            self._add_item(f"  {dot} Collector", enabled=False, dim=True)

        self._add_separator()

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
        self._add_item("  localhost:7880 antwortet nicht", enabled=False, dim=True)
        self._add_item("  → wt web  starten", enabled=False, dim=True)
        self._add_separator()
        self._add_action("Erneut versuchen", "refresh:")
        self._add_separator()
        self._add_action("Beenden", "terminate:")

    # ── Menu helpers ────────────────────────────────────────────

    def _add_header(self, text):
        """Add a bold header item."""
        attrs = {
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(13.0),
        }
        attr_str = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        item.setAttributedTitle_(attr_str)
        item.setEnabled_(False)
        self.menu.addItem_(item)

    def _add_item(self, text, enabled=True, dim=False, color=None):
        """Add a regular menu item."""
        attrs = {}
        if dim:
            attrs[NSForegroundColorAttributeName] = NSColor.secondaryLabelColor()
            attrs[NSFontAttributeName] = NSFont.systemFontOfSize_(12.0)
        else:
            attrs[NSFontAttributeName] = NSFont.monospacedSystemFontOfSize_weight_(11.5, 0.0)

        if color == "red":
            attrs[NSForegroundColorAttributeName] = NSColor.systemRedColor()
        elif color == "green":
            attrs[NSForegroundColorAttributeName] = NSColor.systemGreenColor()
        elif color == "orange":
            attrs[NSForegroundColorAttributeName] = NSColor.systemOrangeColor()

        attr_str = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        item.setAttributedTitle_(attr_str)
        item.setEnabled_(enabled)
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
