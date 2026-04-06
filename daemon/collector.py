#!/usr/bin/env python3
"""WorkTracker Collector Daemon — collects system activity data every 10s."""

import ctypes
import ctypes.util
import fnmatch
import glob as globmod
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yaml
from AppKit import (
    NSApplicationActivationPolicyRegular,
    NSDate,
    NSEvent,
    NSPasteboard,
    NSRunLoop,
    NSRunningApplication,
    NSWorkspace,
    NSWorkspaceDidWakeNotification,
    NSWorkspaceWillSleepNotification,
)
from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    CGEventGetLocation,
    CGEventKeyboardGetUnicodeString,
    CGEventMaskBit,
    CGEventSourceSecondsSinceLastEventType,
    CGEventTapCreate,
    CGEventTapEnable,
    CGWindowListCopyWindowInfo,
    kCFRunLoopCommonModes,
    kCGEventKeyDown,
    kCGEventLeftMouseDown,
    kCGEventMouseMoved,
    kCGEventRightMouseDown,
    kCGEventScrollWheel,
    kCGEventSourceStateCombinedSessionState,
    kCGHeadInsertEventTap,
    kCGNullWindowID,
    kCGSessionEventTap,
    kCGWindowBounds,
    kCGWindowLayer,
    kCGWindowListOptionOnScreenOnly,
    kCGWindowName,
    kCGWindowOwnerName,
    kCGWindowOwnerPID,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LISTEN_ONLY = 1  # kCGEventTapOptionListenOnly

MEDIA_BUNDLES = {
    "com.spotify.client",
    "com.apple.Music",
    "org.videolan.vlc",
    "com.colliderli.iina",
    "com.apple.podcasts",
}
BROWSER_BUNDLES = {
    "com.apple.Safari",
    "com.google.Chrome",
    "org.mozilla.firefox",
    "com.brave.Browser",
    "com.microsoft.edgemac",
    "company.thebrowser.Browser",  # Arc
}
MEDIA_URL_PATTERNS = [
    (re.compile(r"^(.+?)\s*[-\u2013\u2014]\s*YouTube"), "YouTube"),
    (re.compile(r"^(.+?)\s*[-\u2013\u2014]\s*Netflix"), "Netflix"),
    (re.compile(r"^(.+?)\s*[-\u2013\u2014]\s*Twitch"), "Twitch"),
    (re.compile(r"^(.+?)\s*[-\u2013\u2014]\s*Spotify"), "Spotify Web"),
    (re.compile(r"^(.+?)\s*[-\u2013\u2014]\s*SoundCloud"), "SoundCloud"),
    (re.compile(r"^(.+?)\s*[-\u2013\u2014]\s*Apple\s*Music"), "Apple Music Web"),
]
# Bundles whose windows are noise (overlays, app-icon placeholders)
WINDOW_FILTER_BUNDLES = {"com.apple.WindowManager"}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(log_dir: Path, level_name: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("collector")
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))

    fh = logging.handlers.RotatingFileHandler(
        log_dir / "collector.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# Permission check
# ---------------------------------------------------------------------------


def check_accessibility() -> bool:
    """Return True if this process has Accessibility / Input Monitoring permission."""
    try:
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return lib.AXIsProcessTrusted()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# AX (Accessibility) API — true focused app detection
# ---------------------------------------------------------------------------

# Use pyobjc native bindings (HIServices is part of ApplicationServices).
# These handle ObjC ↔ Python bridging correctly, unlike raw ctypes.
from ApplicationServices import (  # noqa: E402
    AXUIElementCreateSystemWide,
    AXUIElementCopyAttributeValue,
    AXUIElementGetPid,
    kAXFocusedApplicationAttribute,
)

# Pre-create the system-wide AXUIElement (singleton, reusable across snapshots)
_ax_system_wide = AXUIElementCreateSystemWide()


def _get_focused_app_via_ax() -> Optional[dict]:
    """Use the Accessibility API to get the app that truly has keyboard focus.

    This correctly identifies the focused app even when a floating overlay
    (like the Claude Desktop Cowork widget) holds 'frontmostApplication' status.
    Returns dict with name/bundle_id/pid or None on failure.
    """
    try:
        err, focused_app_ref = AXUIElementCopyAttributeValue(
            _ax_system_wide, kAXFocusedApplicationAttribute, None
        )
        if err != 0 or focused_app_ref is None:
            return None

        err, pid = AXUIElementGetPid(focused_app_ref, None)
        if err != 0:
            return None

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return None

        return {
            "name": str(app.localizedName() or ""),
            "bundle_id": str(app.bundleIdentifier() or ""),
            "pid": pid,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Input Monitor  (CGEventTap in background thread)
# ---------------------------------------------------------------------------


class InputMonitor(threading.Thread):
    """Counts keystrokes, mouse clicks, scroll events, mouse distance via CGEventTap."""

    def __init__(self, track_content: bool = False):
        super().__init__(daemon=True, name="InputMonitor")
        self._lock = threading.Lock()
        self._keystrokes = 0
        self._clicks_left = 0
        self._clicks_right = 0
        self._scroll_events = 0
        self._mouse_distance = 0.0
        self._last_mx = 0.0
        self._last_my = 0.0
        self._has_last_pos = False
        self._run_loop = None
        self._tap_ok = threading.Event()
        self._tap_failed = threading.Event()
        self._track_content = track_content
        self._keystroke_buffer: list[str] = []

    # -- callback (runs on CFRunLoop thread) --------------------------------

    def _callback(self, proxy, event_type, event, refcon):
        with self._lock:
            if event_type == kCGEventKeyDown:
                self._keystrokes += 1
                if self._track_content:
                    try:
                        length, chars = CGEventKeyboardGetUnicodeString(event, 4, None, None)
                        if length and length > 0 and chars:
                            self._keystroke_buffer.append(str(chars[:length]))
                    except Exception:
                        pass
            elif event_type == kCGEventLeftMouseDown:
                self._clicks_left += 1
            elif event_type == kCGEventRightMouseDown:
                self._clicks_right += 1
            elif event_type == kCGEventScrollWheel:
                self._scroll_events += 1
            elif event_type == kCGEventMouseMoved:
                loc = CGEventGetLocation(event)
                if self._has_last_pos:
                    dx = loc.x - self._last_mx
                    dy = loc.y - self._last_my
                    self._mouse_distance += math.sqrt(dx * dx + dy * dy)
                self._last_mx = loc.x
                self._last_my = loc.y
                self._has_last_pos = True
        return event

    # -- thread entry -------------------------------------------------------

    def run(self):
        mask = (
            CGEventMaskBit(kCGEventKeyDown)
            | CGEventMaskBit(kCGEventLeftMouseDown)
            | CGEventMaskBit(kCGEventRightMouseDown)
            | CGEventMaskBit(kCGEventScrollWheel)
            | CGEventMaskBit(kCGEventMouseMoved)
        )
        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            LISTEN_ONLY,
            mask,
            self._callback,
            None,
        )
        if tap is None:
            self._tap_failed.set()
            return

        self._tap_ok.set()
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        self._run_loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self._run_loop, source, kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)
        CFRunLoopRun()

    def wait_for_status(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._tap_ok.is_set():
                return True
            if self._tap_failed.is_set():
                return False
            time.sleep(0.05)
        return False

    def stop(self):
        if self._run_loop:
            CFRunLoopStop(self._run_loop)

    def reset_and_get(self) -> dict:
        with self._lock:
            r = {
                "keystrokes": self._keystrokes,
                "mouse_distance_px": round(self._mouse_distance),
                "mouse_clicks_left": self._clicks_left,
                "mouse_clicks_right": self._clicks_right,
                "scroll_events": self._scroll_events,
            }
            if self._track_content and self._keystroke_buffer:
                r["keystroke_content"] = "".join(self._keystroke_buffer)
                self._keystroke_buffer.clear()
            self._keystrokes = 0
            self._clicks_left = 0
            self._clicks_right = 0
            self._scroll_events = 0
            self._mouse_distance = 0.0
            return r


# ---------------------------------------------------------------------------
# Clipboard Monitor
# ---------------------------------------------------------------------------


class ClipboardMonitor:
    def __init__(self):
        pb = NSPasteboard.generalPasteboard()
        self._last_count = pb.changeCount()
        self._source_app: Optional[str] = None

    def set_active_app(self, name: Optional[str]):
        self._source_app = name

    def check(self) -> dict:
        pb = NSPasteboard.generalPasteboard()
        count = pb.changeCount()

        if count == self._last_count:
            return {"changed": False}

        self._last_count = count
        result: dict[str, Any] = {"changed": True, "source_app": self._source_app}

        types = pb.types()
        if not types:
            result["type"] = "unknown"
            return result

        types_list = list(types)

        # Files
        if "NSFilenamesPboardType" in types_list or "public.file-url" in types_list:
            filenames = pb.propertyListForType_("NSFilenamesPboardType")
            result["type"] = "file"
            result["content"] = list(filenames) if filenames else []
            result["length"] = len(result["content"])
            return result

        # Text
        text = pb.stringForType_("public.utf8-plain-text")
        if text is not None:
            result["type"] = "text"
            result["content"] = str(text)
            result["length"] = len(result["content"])
            return result

        # Image
        if any(t in types_list for t in ("public.png", "public.tiff", "public.jpeg")):
            data = pb.dataForType_("public.png") or pb.dataForType_("public.tiff")
            result["type"] = "image"
            result["length"] = len(data) if data else 0
            return result

        result["type"] = "other"
        return result


# ---------------------------------------------------------------------------
# Sleep / Wake Monitor
# ---------------------------------------------------------------------------


class SleepWakeMonitor:
    """Track macOS sleep/wake events via NSWorkspace notifications.

    Also detects gaps heuristically: if the main loop iteration took much
    longer than the configured interval, we assume the machine was asleep.
    """

    def __init__(self) -> None:
        self._events: list[dict] = []
        self._lock = threading.Lock()
        try:
            nc = NSWorkspace.sharedWorkspace().notificationCenter()
            nc.addObserver_selector_name_object_(
                self, "onWillSleep:", NSWorkspaceWillSleepNotification, None,
            )
            nc.addObserver_selector_name_object_(
                self, "onDidWake:", NSWorkspaceDidWakeNotification, None,
            )
        except Exception:
            pass  # graceful fallback — heuristic gap detection still works

    # PyObjC callback: called on the thread that owns the notification center
    def onWillSleep_(self, notification: Any) -> None:  # noqa: N802
        with self._lock:
            self._events.append({
                "type": "sleep",
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            })

    def onDidWake_(self, notification: Any) -> None:  # noqa: N802
        with self._lock:
            self._events.append({
                "type": "wake",
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            })

    def record_gap(self, gap_seconds: float) -> None:
        """Record a heuristic gap (wall-clock time >> expected interval)."""
        with self._lock:
            self._events.append({
                "type": "gap",
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "gap_seconds": round(gap_seconds, 1),
            })

    def get_and_clear(self) -> list[dict]:
        with self._lock:
            events = self._events[:]
            self._events.clear()
            return events


# ---------------------------------------------------------------------------
# Browser History Reader
# ---------------------------------------------------------------------------


class BrowserHistoryReader:
    """Reads URLs from browser history SQLite databases."""

    BROWSER_DB_PATHS = {
        "com.google.Chrome": ("~/Library/Application Support/Google/Chrome/Default/History", "chromium"),
        "com.brave.Browser": ("~/Library/Application Support/BraveSoftware/Brave-Browser/Default/History", "chromium"),
        "com.microsoft.edgemac": ("~/Library/Application Support/Microsoft Edge/Default/History", "chromium"),
        "company.thebrowser.Browser": ("~/Library/Application Support/Arc/User Data/Default/History", "chromium"),
        "org.mozilla.firefox": ("~/Library/Application Support/Firefox/Profiles/*/places.sqlite", "firefox"),
        "com.apple.Safari": ("~/Library/Safari/History.db", "safari"),
    }

    def __init__(self):
        self._cache: dict[str, tuple[float, str]] = {}  # bundle_id -> (copy_time, tmp_path)
        self._cache_ttl = 30.0
        self._tmp_dir = tempfile.mkdtemp(prefix="worktracker_browser_")
        self._firefox_profile: Optional[str] = None

    def _resolve_db_path(self, bundle_id: str) -> Optional[str]:
        entry = self.BROWSER_DB_PATHS.get(bundle_id)
        if not entry:
            return None
        raw_path, _ = entry
        expanded = os.path.expanduser(raw_path)
        if "*" in expanded:
            if bundle_id == "org.mozilla.firefox":
                if self._firefox_profile and os.path.exists(self._firefox_profile):
                    return self._firefox_profile
                matches = globmod.glob(expanded)
                if matches:
                    self._firefox_profile = matches[0]
                    return matches[0]
                return None
            matches = globmod.glob(expanded)
            return matches[0] if matches else None
        return expanded if os.path.exists(expanded) else None

    def _get_cached_copy(self, bundle_id: str) -> Optional[str]:
        now = time.monotonic()
        if bundle_id in self._cache:
            copy_time, tmp_path = self._cache[bundle_id]
            if now - copy_time < self._cache_ttl and os.path.exists(tmp_path):
                return tmp_path

        db_path = self._resolve_db_path(bundle_id)
        if not db_path:
            return None

        tmp_path = os.path.join(self._tmp_dir, f"{bundle_id.replace('.', '_')}.db")
        try:
            shutil.copy2(db_path, tmp_path)
            self._cache[bundle_id] = (now, tmp_path)
            return tmp_path
        except Exception:
            return None

    def get_url(self, bundle_id: str, window_title: str) -> Optional[str]:
        if not window_title or bundle_id not in self.BROWSER_DB_PATHS:
            return None
        _, db_type = self.BROWSER_DB_PATHS[bundle_id]
        tmp_db = self._get_cached_copy(bundle_id)
        if not tmp_db:
            return None
        try:
            conn = sqlite3.connect(tmp_db, timeout=2)
            conn.execute("PRAGMA journal_mode=OFF")
            url = None
            # Extract a meaningful title fragment for matching
            title_frag = window_title.split(" — ")[0].split(" - ")[0].strip()[:80]
            if not title_frag:
                conn.close()
                return None

            if db_type == "chromium":
                row = conn.execute(
                    "SELECT url FROM urls WHERE title LIKE ? ORDER BY last_visit_time DESC LIMIT 1",
                    (f"%{title_frag}%",),
                ).fetchone()
                if row:
                    url = row[0]
            elif db_type == "firefox":
                row = conn.execute(
                    "SELECT url FROM moz_places WHERE title LIKE ? ORDER BY last_visit_date DESC LIMIT 1",
                    (f"%{title_frag}%",),
                ).fetchone()
                if row:
                    url = row[0]
            elif db_type == "safari":
                row = conn.execute(
                    "SELECT hi.url FROM history_items hi "
                    "JOIN history_visits hv ON hi.id = hv.history_item "
                    "WHERE hi.url LIKE '%' || ? || '%' OR hi.url IN "
                    "(SELECT url FROM history_items WHERE id IN "
                    "(SELECT history_item FROM history_visits ORDER BY visit_time DESC LIMIT 50)) "
                    "ORDER BY hv.visit_time DESC LIMIT 1",
                    (title_frag[:40],),
                ).fetchone()
                if row:
                    url = row[0]
            conn.close()
            return url
        except Exception:
            return None

    def cleanup(self):
        try:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Git Monitor
# ---------------------------------------------------------------------------


class GitMonitor:
    """Monitors git repositories for branch and commit activity."""

    def __init__(self, repo_paths: list[str], scan_interval: int = 60):
        self._repos: list[Path] = []
        for p in repo_paths:
            rp = Path(os.path.expanduser(p))
            if (rp / ".git").exists():
                self._repos.append(rp)
        self._scan_interval = scan_interval
        self._last_scan = 0.0
        self._cache: dict[str, dict] = {}  # repo_name -> {branch, recent_commits}

    def scan_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_scan < self._scan_interval:
            return
        self._last_scan = now
        for repo in self._repos:
            try:
                self._cache[repo.name] = self._scan_repo(repo)
            except Exception:
                pass

    def _scan_repo(self, repo: Path) -> dict:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=repo,
        ).stdout.strip()

        log_out = subprocess.run(
            ["git", "log", "--since=8 hours ago", "--format=%H|%an|%s|%aI", "--no-merges", "-n", "20"],
            capture_output=True, text=True, timeout=5, cwd=repo,
        ).stdout.strip()

        commits = []
        for line in log_out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "hash": parts[0][:8],
                    "author": parts[1],
                    "message": parts[2],
                    "timestamp": parts[3],
                })

        return {"branch": branch, "recent_commits": commits, "repo_path": str(repo)}

    def get_repo_for_window(self, window_title: str) -> Optional[dict]:
        if not window_title:
            return None
        title_lower = window_title.lower()
        for repo in self._repos:
            if repo.name.lower() in title_lower:
                info = self._cache.get(repo.name)
                if info:
                    return {"repo_name": repo.name, **info}
        return None

    def get_all_repos(self) -> dict:
        return dict(self._cache)


# ---------------------------------------------------------------------------
# Calendar Monitor
# ---------------------------------------------------------------------------


class CalendarMonitor:
    """Reads macOS Calendar events via EventKit."""

    def __init__(self, scan_interval: int = 300):
        self._scan_interval = scan_interval
        self._last_scan = 0.0
        self._events: list[dict] = []
        self._store = None
        self._granted = False
        self._init_tried = False

    def _ensure_store(self) -> bool:
        if self._init_tried:
            return self._granted
        self._init_tried = True
        try:
            from EventKit import EKEntityTypeEvent, EKEventStore
            self._store = EKEventStore.alloc().init()
            # Synchronous permission request — blocks but only on first call
            granted = [False]
            done = threading.Event()

            def callback(g, err):
                granted[0] = bool(g)
                done.set()

            self._store.requestAccessToEntityType_completion_(EKEntityTypeEvent, callback)
            done.wait(timeout=10)
            self._granted = granted[0]
        except Exception:
            self._granted = False
        return self._granted

    def _fetch_today_events(self) -> list[dict]:
        if not self._ensure_store():
            return []
        try:
            from Foundation import NSDate, NSCalendar, NSCalendarUnitYear, NSCalendarUnitMonth, NSCalendarUnitDay
            cal = NSCalendar.currentCalendar()
            comps = cal.components_fromDate_(
                NSCalendarUnitYear | NSCalendarUnitMonth | NSCalendarUnitDay,
                NSDate.date(),
            )
            comps.setHour_(0)
            comps.setMinute_(0)
            comps.setSecond_(0)
            start = cal.dateFromComponents_(comps)
            comps.setHour_(23)
            comps.setMinute_(59)
            comps.setSecond_(59)
            end = cal.dateFromComponents_(comps)

            pred = self._store.predicateForEventsWithStartDate_endDate_calendars_(start, end, None)
            events = self._store.eventsMatchingPredicate_(pred) or []

            result = []
            for ev in events:
                if ev.isAllDay():
                    continue
                attendees = ev.attendees() or []
                result.append({
                    "title": str(ev.title() or ""),
                    "start": str(ev.startDate()),
                    "end": str(ev.endDate()),
                    "calendar": str(ev.calendar().title()) if ev.calendar() else "",
                    "attendees": len(attendees),
                })
            return result
        except Exception:
            return []

    def get_events_if_due(self) -> list[dict]:
        now = time.monotonic()
        if now - self._last_scan >= self._scan_interval:
            self._last_scan = now
            self._events = self._fetch_today_events()
        return self._events

    def get_current_event(self) -> Optional[dict]:
        now = datetime.now()
        for ev in self._events:
            try:
                # Parse the event start/end times
                start_str = ev["start"]
                end_str = ev["end"]
                # NSDate string format: "2026-04-06 09:00:00 +0000"
                for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S +0000"):
                    try:
                        start = datetime.strptime(start_str, fmt)
                        end = datetime.strptime(end_str, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    continue
                if start.replace(tzinfo=None) <= now <= end.replace(tzinfo=None):
                    return ev
            except Exception:
                continue
        return None


# ---------------------------------------------------------------------------
# Distraction Notifier
# ---------------------------------------------------------------------------


class DistractionNotifier:
    """Sends macOS notifications when user spends too long on distraction categories."""

    def __init__(self, config: dict, patterns_path: Path):
        notif_cfg = config.get("collector", {}).get("notifications", {})
        self._enabled = notif_cfg.get("enabled", False)
        self._threshold = notif_cfg.get("threshold_minutes", 15) * 60
        self._cooldown = notif_cfg.get("cooldown_minutes", 30) * 60
        self._distraction_cats = set(notif_cfg.get("distraction_categories", []))

        # Load project patterns for category matching
        self._projects: dict = {}
        self._default_project = "Other"
        try:
            with open(patterns_path) as f:
                data = yaml.safe_load(f) or {}
            self._projects = data.get("projects", {})
            self._default_project = data.get("default_project", "Other")
        except Exception:
            pass

        self._streak_start: Optional[float] = None
        self._streak_category: Optional[str] = None
        self._notified_current = False
        self._last_notification_time = 0.0

    def _match_category(self, title: str) -> Optional[str]:
        if not title:
            return None
        title_lower = title.lower()
        for proj_name, proj_info in self._projects.items():
            for pattern in proj_info.get("patterns", []):
                if fnmatch.fnmatch(title_lower, pattern.lower()):
                    return proj_info.get("category", "")
        return None

    def check(self, active_app: Optional[dict], visible_windows: list) -> None:
        if not self._enabled:
            return
        title = active_app.get("window_title", "") if active_app else ""
        category = self._match_category(title)
        now = time.monotonic()

        if category in self._distraction_cats:
            if self._streak_start is None or self._streak_category != category:
                self._streak_start = now
                self._streak_category = category
                self._notified_current = False

            elapsed = now - self._streak_start
            if (elapsed >= self._threshold
                    and not self._notified_current
                    and now - self._last_notification_time >= self._cooldown):
                minutes = int(elapsed / 60)
                self._send_notification(category, minutes)
                self._notified_current = True
                self._last_notification_time = now
        else:
            self._streak_start = None
            self._streak_category = None
            self._notified_current = False

    def _send_notification(self, category: str, minutes: int) -> None:
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "Du bist seit {minutes} Minuten auf {category}." '
                 f'with title "WorkTracker" subtitle "Ablenkungswarnung"'],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Data Collectors
# ---------------------------------------------------------------------------


def _window_title_for_pid(pid: int) -> Optional[str]:
    """Find the first normal-layer window title for a given PID."""
    try:
        windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        if windows:
            for w in windows:
                if w.get(kCGWindowOwnerPID) == pid and w.get(kCGWindowLayer, 99) == 0:
                    t = w.get(kCGWindowName)
                    if t:
                        return str(t)
    except Exception:
        pass
    return None


def collect_active_app() -> Optional[dict]:
    # Primary: use Accessibility API for true keyboard-focus owner.
    # This correctly ignores floating overlays (e.g. Claude Cowork widget)
    # that hold macOS "frontmostApplication" status without actual focus.
    ax = _get_focused_app_via_ax()
    if ax is not None:
        title = _window_title_for_pid(ax["pid"])
        return {"name": ax["name"], "bundle_id": ax["bundle_id"], "window_title": title}

    # Fallback: NSWorkspace frontmostApplication (original behaviour)
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None

        pid = app.processIdentifier()
        name = str(app.localizedName() or "")
        bundle_id = str(app.bundleIdentifier() or "")
        title = _window_title_for_pid(pid)

        return {"name": name, "bundle_id": bundle_id, "window_title": title}
    except Exception:
        return None


def collect_visible_windows() -> list:
    result = []
    try:
        ws = NSWorkspace.sharedWorkspace()

        # Use AX-based focused app for is_active, consistent with collect_active_app()
        ax = _get_focused_app_via_ax()
        if ax is not None:
            focused_pid = ax["pid"]
        else:
            front = ws.frontmostApplication()
            focused_pid = front.processIdentifier() if front else -1

        windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        if not windows:
            return result

        pid_bundle: dict[int, str] = {}
        for a in ws.runningApplications():
            pid_bundle[a.processIdentifier()] = str(a.bundleIdentifier() or "")

        for w in windows:
            if w.get(kCGWindowLayer, 99) != 0:
                continue
            owner = w.get(kCGWindowOwnerName)
            if not owner:
                continue

            pid = w.get(kCGWindowOwnerPID, 0)
            bid = pid_bundle.get(pid, "")

            # Filter out noise windows (WindowManager overlays etc.)
            if bid in WINDOW_FILTER_BUNDLES:
                continue

            b = w.get(kCGWindowBounds, {})

            result.append(
                {
                    "app": str(owner),
                    "bundle_id": bid,
                    "title": str(w.get(kCGWindowName) or ""),
                    "is_active": pid == focused_pid,
                    "position": {"x": int(b.get("X", 0)), "y": int(b.get("Y", 0))},
                    "size": {"w": int(b.get("Width", 0)), "h": int(b.get("Height", 0))},
                }
            )
    except Exception:
        pass
    return result


def collect_running_apps() -> list:
    result = []
    try:
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if app.activationPolicy() != NSApplicationActivationPolicyRegular:
                continue
            result.append(
                {
                    "name": str(app.localizedName() or ""),
                    "bundle_id": str(app.bundleIdentifier() or ""),
                    "active": bool(app.isActive()),
                    "hidden": bool(app.isHidden()),
                }
            )
    except Exception:
        pass
    return result


def collect_media(visible_windows: list) -> Optional[dict]:
    """Best-effort media detection from window titles."""
    for w in visible_windows:
        bid = w.get("bundle_id", "")
        title = w.get("title", "")
        if not title:
            continue

        # Spotify: "Artist - Track" when playing, "Spotify" when idle
        if bid == "com.spotify.client" and title not in ("Spotify", ""):
            parts = re.split(r"\s*[\u2013\u2014-]\s*", title, maxsplit=1)
            if len(parts) >= 2:
                return {"title": parts[1], "artist": parts[0], "app": "Spotify", "state": "playing"}
            return {"title": title, "artist": None, "app": "Spotify", "state": "playing"}

        if bid == "com.apple.Music" and title not in ("Music", "Musik", ""):
            parts = re.split(r"\s*[\u2013\u2014-]\s*", title, maxsplit=1)
            if len(parts) >= 2:
                return {"title": parts[1], "artist": parts[0], "app": "Music", "state": "unknown"}
            return {"title": title, "artist": None, "app": "Music", "state": "unknown"}

        if bid in ("org.videolan.vlc", "com.colliderli.iina"):
            app_label = "VLC" if "vlc" in bid else "IINA"
            clean = re.sub(r"\s*[\u2013\u2014-]\s*(VLC media player|IINA)\s*$", "", title)
            if clean and clean not in (app_label, "VLC media player"):
                return {"title": clean, "artist": None, "app": app_label, "state": "playing"}

    # Browser media sites
    for w in visible_windows:
        bid = w.get("bundle_id", "")
        title = w.get("title", "")
        if bid not in BROWSER_BUNDLES or not title:
            continue
        for pattern, service in MEDIA_URL_PATTERNS:
            m = pattern.match(title)
            if m:
                return {
                    "title": m.group(1).strip(),
                    "artist": None,
                    "app": w.get("app", ""),
                    "state": "unknown",
                    "service": service,
                }
    return None


def collect_idle() -> dict:
    s = kCGEventSourceStateCombinedSessionState
    try:
        kb = CGEventSourceSecondsSinceLastEventType(s, kCGEventKeyDown)
    except Exception:
        kb = -1
    try:
        mouse = min(
            CGEventSourceSecondsSinceLastEventType(s, kCGEventMouseMoved),
            CGEventSourceSecondsSinceLastEventType(s, kCGEventLeftMouseDown),
            CGEventSourceSecondsSinceLastEventType(s, kCGEventRightMouseDown),
            CGEventSourceSecondsSinceLastEventType(s, kCGEventScrollWheel),
        )
    except Exception:
        mouse = -1
    return {
        "idle_seconds_keyboard": round(kb, 1),
        "idle_seconds_mouse": round(mouse, 1),
    }


def collect_mouse_position() -> dict:
    try:
        loc = NSEvent.mouseLocation()
        return {"x": round(loc.x), "y": round(loc.y)}
    except Exception:
        return {"x": 0, "y": 0}


def collect_system() -> dict:
    result: dict[str, Any] = {
        "active_space": None,
        "battery_pct": None,
        "battery_charging": None,
        "brightness": None,
    }

    # Active space (private CGS API)
    try:
        cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        cg.CGSMainConnectionID.restype = ctypes.c_int
        cg.CGSGetActiveSpace.restype = ctypes.c_int
        cg.CGSGetActiveSpace.argtypes = [ctypes.c_int]
        conn = cg.CGSMainConnectionID()
        result["active_space"] = cg.CGSGetActiveSpace(conn)
    except Exception:
        pass

    # Battery
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=2
        ).stdout
        m = re.search(r"(\d+)%;\s*(\w+)", out)
        if m:
            result["battery_pct"] = int(m.group(1))
            result["battery_charging"] = m.group(2).lower() in ("charging", "charged")
    except Exception:
        pass

    # Brightness (CoreDisplay, may not work on external-only setups)
    try:
        cd = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreDisplay.framework/CoreDisplay"
        )
        cd.CoreDisplay_Display_GetUserBrightness.restype = ctypes.c_double
        cd.CoreDisplay_Display_GetUserBrightness.argtypes = [ctypes.c_uint32]
        val = cd.CoreDisplay_Display_GetUserBrightness(0)
        if 0.0 <= val <= 1.0:
            result["brightness"] = round(val, 2)
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Snapshot assembly & writing
# ---------------------------------------------------------------------------


def collect_snapshot(
    input_monitor: Optional[InputMonitor],
    clipboard_monitor: ClipboardMonitor,
    config: dict,
    browser_history: Optional[BrowserHistoryReader] = None,
    git_monitor: Optional[GitMonitor] = None,
    calendar_monitor: Optional[CalendarMonitor] = None,
    sleep_wake_monitor: Optional[SleepWakeMonitor] = None,
) -> dict:
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    cfg = config.get("collector", {})

    active = collect_active_app()
    clipboard_monitor.set_active_app(active["name"] if active else None)

    windows = collect_visible_windows() if cfg.get("track_all_windows", True) else []
    apps = collect_running_apps()
    media = collect_media(windows) if cfg.get("track_media", True) else None

    # Input
    input_data: dict[str, Any] = {}
    if cfg.get("track_input_counts", True):
        if input_monitor:
            input_data = input_monitor.reset_and_get()
        input_data.update(collect_idle())
        input_data["mouse_position"] = collect_mouse_position()

    # Clipboard
    clip = (
        clipboard_monitor.check()
        if cfg.get("track_clipboard_content", True)
        else {"changed": False}
    )

    system = collect_system()

    # Browser URL
    if browser_history and active and active.get("bundle_id") in BROWSER_BUNDLES:
        url = browser_history.get_url(active["bundle_id"], active.get("window_title", ""))
        if url:
            active["url"] = url

    # Git
    git_data = None
    if git_monitor:
        git_monitor.scan_if_due()
        title = active.get("window_title", "") if active else ""
        repo_info = git_monitor.get_repo_for_window(title)
        if repo_info:
            git_data = {
                "repo": repo_info["repo_name"],
                "branch": repo_info["branch"],
                "recent_commits_count": len(repo_info.get("recent_commits", [])),
            }

    # Calendar
    calendar_data = None
    if calendar_monitor:
        calendar_monitor.get_events_if_due()
        current_event = calendar_monitor.get_current_event()
        if current_event:
            calendar_data = {
                "in_meeting": True,
                "event_title": current_event["title"],
                "event_calendar": current_event["calendar"],
                "attendee_count": current_event["attendees"],
            }
        else:
            calendar_data = {"in_meeting": False}

    # Sleep / wake events since last snapshot
    sleep_wake = None
    if sleep_wake_monitor:
        events = sleep_wake_monitor.get_and_clear()
        if events:
            sleep_wake = events

    return {
        "ts": ts,
        "active_app": active,
        "visible_windows": windows,
        "running_apps": apps,
        "media": media,
        "input": input_data or None,
        "clipboard": clip,
        "system": system,
        "git": git_data,
        "calendar": calendar_data,
        "sleep_wake": sleep_wake,
    }


def write_snapshot(snapshot: dict, data_dir: Path) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    path = data_dir / f"{today}.jsonl"
    line = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    config_path = Path("~/WorkTracker/daemon/config.yaml").expanduser()
    config = load_config(config_path)
    cfg = config.get("collector", {})
    patterns_path = Path("~/WorkTracker/daemon/project_patterns.yaml").expanduser()

    log_dir = Path(cfg.get("log_dir", "~/WorkTracker/logs")).expanduser()
    log_level = cfg.get("log_level", "INFO")
    logger = setup_logging(log_dir, log_level)

    data_dir = Path(cfg.get("data_dir", "~/WorkTracker/data/snapshots")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    interval = cfg.get("interval_seconds", 10)

    logger.info("=" * 60)
    logger.info("WorkTracker Collector starting")
    logger.info("Config: interval=%ds  data_dir=%s", interval, data_dir)

    # ── Permission check ──────────────────────────────────────────────
    has_ax = check_accessibility()
    logger.info("AXIsProcessTrusted: %s", has_ax)

    # ── Input monitor (try regardless — AXIsProcessTrusted can lie) ───
    input_monitor: Optional[InputMonitor] = None
    if cfg.get("track_input_counts", True):
        track_content = cfg.get("track_keystroke_content", False)
        input_monitor = InputMonitor(track_content=track_content)
        input_monitor.start()
        if input_monitor.wait_for_status(3.0):
            logger.info("Input monitor (CGEventTap): OK  content_tracking=%s", track_content)
        else:
            logger.warning(
                "CGEventTap failed — input counting disabled.\n"
                "  -> System Settings > Privacy & Security > Accessibility\n"
                "  -> System Settings > Privacy & Security > Input Monitoring\n"
                "  Add: %s  (realpath: %s)",
                sys.executable,
                os.path.realpath(sys.executable),
            )
            input_monitor = None

    # ── Clipboard monitor ─────────────────────────────────────────────
    clipboard_monitor = ClipboardMonitor()
    logger.info("Clipboard monitor: OK")

    # ── Sleep / wake monitor ─────────────────────────────────────────
    sleep_wake_monitor = SleepWakeMonitor()
    logger.info("Sleep/wake monitor: OK")

    # ── Browser history reader ────────────────────────────────────────
    browser_history: Optional[BrowserHistoryReader] = None
    if cfg.get("track_browser_urls", False):
        browser_history = BrowserHistoryReader()
        logger.info("Browser history reader: OK")

    # ── Git monitor ───────────────────────────────────────────────────
    git_monitor: Optional[GitMonitor] = None
    if cfg.get("track_git", False):
        git_repos = cfg.get("git_repos", [])
        git_interval = cfg.get("git_scan_interval_seconds", 60)
        git_monitor = GitMonitor(git_repos, git_interval)
        logger.info("Git monitor: %d repos configured", len(git_monitor._repos))

    # ── Calendar monitor ──────────────────────────────────────────────
    calendar_monitor: Optional[CalendarMonitor] = None
    if cfg.get("track_calendar", False):
        calendar_monitor = CalendarMonitor(scan_interval=300)
        logger.info("Calendar monitor: OK (EventKit)")

    # ── Distraction notifier ──────────────────────────────────────────
    distraction_notifier: Optional[DistractionNotifier] = None
    notif_cfg = cfg.get("notifications", {})
    if notif_cfg.get("enabled", False):
        distraction_notifier = DistractionNotifier(config, patterns_path)
        logger.info(
            "Distraction notifier: ON (threshold=%dmin, cooldown=%dmin)",
            notif_cfg.get("threshold_minutes", 15),
            notif_cfg.get("cooldown_minutes", 30),
        )

    # ── System probes ─────────────────────────────────────────────────
    sys_info = collect_system()
    parts = [
        "space=%s" % ("OK" if sys_info["active_space"] else "N/A"),
        "battery=%s" % ("OK" if sys_info["battery_pct"] is not None else "N/A"),
        "brightness=%s" % ("OK" if sys_info["brightness"] is not None else "N/A"),
    ]
    logger.info("System sources: %s", "  ".join(parts))
    logger.info("Collector ready — entering main loop")

    # ── Signal handling ───────────────────────────────────────────────
    shutdown = threading.Event()

    def _on_signal(signum, _frame):
        name = signal.Signals(signum).name
        logger.info("Received %s — shutting down", name)
        shutdown.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # ── Main loop ─────────────────────────────────────────────────────
    n = 0
    last_wall = time.time()
    gap_factor = 3  # wall-clock > 3× interval → heuristic gap
    while not shutdown.is_set():
        t0 = time.monotonic()

        # Heuristic sleep/wake gap detection
        now_wall = time.time()
        wall_delta = now_wall - last_wall
        if wall_delta > interval * gap_factor and n > 0:
            logger.info("Detected gap of %.0fs (likely sleep)", wall_delta)
            sleep_wake_monitor.record_gap(wall_delta)
        last_wall = now_wall

        try:
            snap = collect_snapshot(
                input_monitor, clipboard_monitor, config,
                browser_history=browser_history,
                git_monitor=git_monitor,
                calendar_monitor=calendar_monitor,
                sleep_wake_monitor=sleep_wake_monitor,
            )
            write_snapshot(snap, data_dir)
            n += 1
            if n == 1:
                logger.info("First snapshot written to %s", data_dir)

            # Distraction check (runs after snapshot)
            if distraction_notifier:
                distraction_notifier.check(
                    snap.get("active_app"),
                    snap.get("visible_windows", []),
                )
        except Exception:
            logger.exception("Snapshot error")

        elapsed = time.monotonic() - t0
        # Process NSRunLoop in short bursts so that NSWorkspace
        # receives workspace-change notifications (frontmostApplication etc.)
        # while still responding promptly to shutdown signals.
        remaining = max(0.0, interval - elapsed)
        while remaining > 0 and not shutdown.is_set():
            tick = min(remaining, 0.5)
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(tick)
            )
            remaining -= tick

    # ── Cleanup ───────────────────────────────────────────────────────
    if input_monitor:
        input_monitor.stop()
    if browser_history:
        browser_history.cleanup()
    logger.info("Collector stopped after %d snapshots", n)


if __name__ == "__main__":
    main()
