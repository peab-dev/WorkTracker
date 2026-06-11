#!/usr/bin/env python3
"""WorkTracker Web Dashboard — Flask Server"""

import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, request, send_from_directory
import subprocess

from aggregator import aggregate_topics
from web_categories import build_web_category_tree, classify_url
import categories as catpool

app = Flask(__name__)


def sanitize_for_json(obj):
    """Replace NaN/Inf floats with None so JSON serialization works."""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    return obj

import fnmatch
import yaml

BASE = Path.home() / "WorkTracker"
PATTERNS_FILE = BASE / "daemon" / "project_patterns.yaml"
PATTERNS_DEFAULT_FILE = BASE / "daemon" / "project_patterns.default.yaml"
CONFIG_FILE = BASE / "daemon" / "config.yaml"
CONFIG_DEFAULT_FILE = BASE / "daemon" / "config.default.yaml"


def _paths_from_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = yaml.safe_load(f) or {}
        col = cfg.get("collector", {})
        agg = cfg.get("aggregator", {})
        summaries = Path(agg["summaries_dir"]).expanduser()
        return (
            Path(col["data_dir"]),
            Path(agg["sessions_dir"]),
            Path(col["screenshot"]["dir"]),
            summaries,
            Path(col["log_dir"]),
        )
    except Exception:
        summaries = BASE / "summaries"
        return (
            BASE / "data" / "snapshots",
            BASE / "data" / "sessions",
            BASE / "data" / "screenshots",
            summaries,
            BASE / "logs",
        )


DATA_SNAP, DATA_SESS, DATA_SCREENSHOTS, SUMMARIES, LOGS = _paths_from_config()

# Apps that represent inactive/lock-screen state — excluded from stats
INACTIVE_APPS = {"loginwindow"}


def _ensure_user_config() -> None:
    """Bootstrap config.yaml from config.default.yaml on first run."""
    if CONFIG_FILE.exists() or not CONFIG_DEFAULT_FILE.exists():
        return
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(CONFIG_DEFAULT_FILE.read_text())


def _load_app_categories():
    """Load app_categories from default + user project_patterns files.

    Default values provide the baseline; any user-file keys override
    per category name.
    """
    merged: dict = {}
    for path in (PATTERNS_DEFAULT_FILE, PATTERNS_FILE):
        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            merged.update(data.get("app_categories") or {})
        except Exception:
            continue
    return merged


import unicodedata
import re

_INVISIBLE_RE = re.compile(
    r"[\u200e\u200f\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064"
    r"\ufeff\u00ad\u034f\u061c\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


def _clean_name(name):
    """Strip invisible unicode chars (LRM, soft-hyphen, ZWS, etc.)."""
    return _INVISIBLE_RE.sub("", name or "")


def classify_app(app_name, categories=None):
    """Return the category for an app name using current config."""
    if categories is None:
        categories = _load_app_categories()
    return catpool.classify_app(app_name, categories)


# ── Daten ────────────────────────────────────────────────────


def tail_jsonl(path, n=30):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            if fsize == 0:
                return []
            f.seek(max(0, fsize - n * 8192))
            lines = f.read().decode("utf-8", errors="replace").strip().split("\n")
            result = []
            for l in lines[-n:]:
                try:
                    result.append(json.loads(l))
                except json.JSONDecodeError:
                    pass
            return result
    except FileNotFoundError:
        return []


def load_sessions(date_str):
    path = DATA_SESS / f"{date_str}.json"
    try:
        with open(path) as f:
            sessions = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    # Kategorie-Pool: Legacy-Namen beim Lesen kanonisieren; ältere Sessions
    # ohne activity_category bekommen sie abgeleitet (Projekt → Web → Tool).
    for s in sessions:
        if s.get("category"):
            s["category"] = catpool.canonical(s["category"])
        if s.get("web_category"):
            s["web_category"] = catpool.canonical(s["web_category"])
        if not s.get("activity_category"):
            s["activity_category"] = catpool.derive_activity_category(s)
    return sessions


def _dates_in_range(start_str, end_str):
    """Yield YYYY-MM-DD strings from start..end inclusive."""
    start = datetime.strptime(start_str, "%Y-%m-%d").date()
    end = datetime.strptime(end_str, "%Y-%m-%d").date()
    if end < start:
        start, end = end, start
    d = start
    one = timedelta(days=1)
    while d <= end:
        yield d.strftime("%Y-%m-%d")
        d += one


_NO_TOPIC = "(ohne Topic)"
_NO_PROJECT = "(ohne Projekt)"
_NO_APP = "(unbekannte App)"


def aggregate_triples(sessions):
    """Group sessions by (topic, project, app_name).

    Missing/empty values get placeholder labels so they still appear in
    visualizations. Returns list sorted by duration desc.
    """
    buckets = {}
    proj_cat = {}  # project -> Kategorie (letzter nicht-leerer Wert gewinnt)
    for s in sessions:
        topic = (s.get("topic") or "").strip() or _NO_TOPIC
        project = (s.get("project") or "").strip() or _NO_PROJECT
        app_name = (s.get("app_name") or "").strip() or _NO_APP
        dur = int(s.get("duration_seconds", 0) or 0)
        if dur <= 0:
            continue
        cat = (s.get("category") or "").strip()
        if cat:
            proj_cat[project] = cat
        key = (topic, project, app_name)
        if key not in buckets:
            buckets[key] = {"sec": 0, "count": 0}
        buckets[key]["sec"] += dur
        buckets[key]["count"] += 1
    items = [
        {"topic": t, "project": p, "app": a, "sec": v["sec"], "count": v["count"],
         "category": proj_cat.get(p, "")}
        for (t, p, a), v in buckets.items()
    ]
    items.sort(key=lambda x: x["sec"], reverse=True)
    return items


def snapshot_count(date_str):
    path = DATA_SNAP / f"{date_str}.jsonl"
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def _pgrep_process(pattern):
    """Return (pid, started_epoch) for the first process matching *pattern*.

    Uses ``ps -Ao pid,lstart,command`` so it finds both launchd-managed and
    plain user-launched processes (e.g. when collector is started via
    ``wt start`` instead of launchctl).
    """
    try:
        r = subprocess.run(
            ["ps", "-Axo", "pid=,lstart=,command="],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return None
        from datetime import datetime as _dt
        for line in r.stdout.splitlines():
            line = line.strip()
            if pattern not in line:
                continue
            # Format: "12345 Wed Apr 15 01:23:45 2026 /path/to/cmd args"
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            rest = parts[1]
            # lstart is a fixed 24-char field in ps output format "%c"
            # e.g. "Wed Apr 15 01:23:45 2026"
            tokens = rest.split(None, 5)
            if len(tokens) < 6:
                continue
            lstart_str = " ".join(tokens[:5])
            try:
                started = _dt.strptime(lstart_str, "%a %b %d %H:%M:%S %Y")
                return (pid, started.timestamp())
            except ValueError:
                return (pid, None)
    except Exception:
        pass
    return None


def launchd_status(label):
    """Return status for a launchd label with a pgrep fallback.

    Fields:
      loaded:     bool — launchctl knows about this label
      running:    bool — there is an active process for this collector
      pid:        int|None — PID of the running process
      started_at: float|None — epoch seconds when the process started
      uptime_sec: int|None — seconds since the process started
      exit:       int|None — last exit status (launchd only)
    """
    info = {
        "loaded": False, "running": False, "pid": None,
        "started_at": None, "uptime_sec": None, "exit": None,
    }

    # 1. Primary path: launchctl for properly installed services.
    try:
        r = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            info["loaded"] = True
            for line in r.stdout.split("\n"):
                if "LastExitStatus" in line:
                    for tok in line.replace('"', "").replace(";", "").split():
                        try:
                            info["exit"] = int(tok)
                        except ValueError:
                            pass
            lines = r.stdout.strip().split("\n")
            if len(lines) >= 2:
                tok = lines[1].split()
                if tok and tok[0] != "-":
                    try:
                        info["pid"] = int(tok[0])
                    except ValueError:
                        pass
    except Exception:
        pass

    # 2. Fallback: pgrep for plain user-launched processes.
    #    Map known labels → process-search patterns.
    if not info["pid"]:
        pattern = None
        if "collector" in label:
            pattern = "daemon/collector.py"
        elif "aggregator" in label:
            pattern = "daemon/aggregator.py"
        if pattern:
            found = _pgrep_process(pattern)
            if found:
                info["pid"], started = found
                info["started_at"] = started

    # 3. If we have a PID, get its start time via ps (if we didn't already).
    if info["pid"] and info["started_at"] is None:
        try:
            from datetime import datetime as _dt
            r = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(info["pid"])],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode == 0 and r.stdout.strip():
                started = _dt.strptime(r.stdout.strip(), "%a %b %d %H:%M:%S %Y")
                info["started_at"] = started.timestamp()
        except Exception:
            pass

    if info["pid"]:
        info["running"] = True
        if info["started_at"]:
            info["uptime_sec"] = int(datetime.now().timestamp() - info["started_at"])

    return info


def latest_report(report_type):
    d = SUMMARIES / report_type
    try:
        files = sorted(d.glob("*.md"))
        if files:
            f = files[-1]
            st = f.stat()
            return {
                "name": f.name,
                "size": st.st_size,
                "path": str(f),
                "mtime": st.st_mtime,
            }
    except Exception:
        pass
    return None


def latest_report_group(report_type):
    """Return the latest group of files (raw, summary, short-summary) for a report type."""
    d = SUMMARIES / report_type
    try:
        # Find the latest base date by looking at all .md files
        all_files = sorted(d.glob("*.md"))
        if not all_files:
            return None

        # Get unique base prefixes (e.g. "2026-04-06", "2026-W15", "2026-04")
        bases = set()
        for f in all_files:
            name = f.name
            if name.startswith("."):
                continue
            if name.endswith("-short-summary.md"):
                bases.add(name.replace("-short-summary.md", ""))
            elif name.endswith("-summary.md"):
                bases.add(name.replace("-summary.md", ""))
            else:
                bases.add(name.replace(".md", ""))

        if not bases:
            return None

        latest_base = sorted(bases)[-1]
        group = {}
        for suffix, label in [(".md", "raw"), ("-summary.md", "summary"), ("-short-summary.md", "short")]:
            f = d / (latest_base + suffix)
            if f.exists():
                st = f.stat()
                group[label] = {
                    "name": f.name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
        return group if group else None
    except Exception:
        return None


def all_reports(report_type):
    d = SUMMARIES / report_type
    try:
        files = sorted(d.glob("*.md"), reverse=True)
        result = []
        for f in files:
            st = f.stat()
            result.append({
                "name": f.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        return result
    except Exception:
        return []


def log_tail(path, n=5):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            if fsize == 0:
                return []
            f.seek(max(0, fsize - 4096))
            lines = f.read().decode("utf-8", errors="replace").strip().split("\n")
            return lines[-n:]
    except FileNotFoundError:
        return []


# ── API ──────────────────────────────────────────────────────


@app.route("/api/live")
def api_live():
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Load config for interval (bootstraps user file on first run)
    try:
        _ensure_user_config()
        import yaml as _yaml
        with open(CONFIG_FILE) as _cf:
            _cfg = _yaml.safe_load(_cf) or {}
        interval = _cfg.get("collector", {}).get("interval_seconds", 10)
    except Exception:
        interval = 10

    snaps = tail_jsonl(DATA_SNAP / f"{today}.jsonl", 30)
    latest = snaps[-1] if snaps else None
    sessions = load_sessions(today)
    snap_total = snapshot_count(today)

    # Services
    services = {
        "collector": launchd_status("com.peab.worktracker.collector"),
        "agg_daily": launchd_status("com.peab.worktracker.aggregator.daily"),
        "agg_weekly": launchd_status("com.peab.worktracker.aggregator.weekly"),
        "agg_monthly": launchd_status("com.peab.worktracker.aggregator.monthly"),
    }

    # Live activity
    live = None
    if latest:
        aa = latest.get("active_app", {})
        inp = latest.get("input", {})

        # Input rates from last N snapshots
        recent = snaps[-6:]
        span = len(recent) * interval
        keys_pm = int(sum(s.get("input", {}).get("keystrokes", 0) for s in recent) * 60 / span) if span else 0
        clicks_pm = int(sum(
            s.get("input", {}).get("mouse_clicks_left", 0) +
            s.get("input", {}).get("mouse_clicks_right", 0)
            for s in recent
        ) * 60 / span) if span else 0
        scroll_pm = int(sum(s.get("input", {}).get("scroll_events", 0) for s in recent) * 60 / span) if span else 0

        live = {
            "app": aa.get("name"),
            "window": aa.get("window_title"),
            "media": latest.get("media"),
            "keys_pm": keys_pm,
            "clicks_pm": clicks_pm,
            "scroll_pm": scroll_pm,
            "idle_kb": inp.get("idle_seconds_keyboard", 0),
            "idle_ms": inp.get("idle_seconds_mouse", 0),
            "system": latest.get("system"),
            "git": latest.get("git"),
            "ts": latest.get("ts"),
        }

    # Daily statistics (exclude lock-screen / inactive apps)
    day_stats = None
    active_sessions = [s for s in sessions if s.get("app_name") not in INACTIVE_APPS]
    if active_sessions:
        total_sec = sum(s.get("duration_seconds", 0) for s in active_sessions)
        focus = [s for s in active_sessions if s.get("duration_seconds", 0) >= 1500]
        focus_sec = sum(s.get("duration_seconds", 0) for s in focus)
        apps = [s.get("app_name", "") for s in active_sessions]
        switches = sum(1 for i in range(1, len(apps)) if apps[i] != apps[i - 1])
        clip = sum(len(s.get("clipboard_events", [])) for s in active_sessions)
        keys = sum(s.get("keystrokes_total", 0) for s in active_sessions)
        clicks = sum(s.get("mouse_clicks_total", 0) for s in active_sessions)
        scrolls = sum(s.get("scroll_events_total", 0) for s in active_sessions)

        # Projects (exclude "Other")
        projects = {}
        for s in active_sessions:
            p = s.get("project", "Other")
            if p == "Other":
                continue
            if p not in projects:
                projects[p] = {"sec": 0, "n": 0, "intensity": []}
            projects[p]["sec"] += s.get("duration_seconds", 0)
            projects[p]["n"] += 1
            i = s.get("intensity_score")
            if i is not None:
                projects[p]["intensity"].append(i)

        proj_total_sec = sum(pd["sec"] for pd in projects.values())
        proj_list = []
        for pn, pd in sorted(projects.items(), key=lambda x: x[1]["sec"], reverse=True):
            avg_i = sum(pd["intensity"]) / len(pd["intensity"]) if pd["intensity"] else 0
            proj_list.append({
                "name": pn,
                "sec": pd["sec"],
                "pct": round(pd["sec"] / proj_total_sec * 100, 1) if proj_total_sec else 0,
                "sessions": pd["n"],
                "intensity": round(avg_i, 1),
            })

        # App categories with per-app breakdown (live from config)
        _app_cats = _load_app_categories()
        cat_times = {}
        cat_apps = {}  # {category: {app_name: sec}}
        for s in active_sessions:
            a = s.get("app_name", "Unknown")
            c = classify_app(a, _app_cats)
            dur = s.get("duration_seconds", 0)
            if c not in cat_times:
                cat_times[c] = {"sec": 0, "n": 0}
            cat_times[c]["sec"] += dur
            cat_times[c]["n"] += 1
            if c not in cat_apps:
                cat_apps[c] = {}
            cat_apps[c][a] = cat_apps[c].get(a, 0) + dur
        cat_list = []
        for c, d in sorted(cat_times.items(), key=lambda x: x[1]["sec"], reverse=True):
            apps_in_cat = [
                {"name": a, "sec": s, "pct": round(s / total_sec * 100, 1)}
                for a, s in sorted(cat_apps.get(c, {}).items(), key=lambda x: x[1], reverse=True)
            ]
            cat_list.append({
                "name": c, "sec": d["sec"],
                "pct": round(d["sec"] / total_sec * 100, 1),
                "sessions": d["n"],
                "apps": apps_in_cat,
            })

        # Apps
        app_times = {}
        for s in active_sessions:
            a = s.get("app_name", "Unknown")
            app_times[a] = app_times.get(a, 0) + s.get("duration_seconds", 0)
        app_list = [
            {"name": a, "sec": t, "pct": round(t / total_sec * 100, 1)}
            for a, t in sorted(app_times.items(), key=lambda x: x[1], reverse=True)
        ]

        # Hourly activity
        hourly = [0] * 24
        for s in active_sessions:
            try:
                h = datetime.fromisoformat(s["start"]).hour
                hourly[h] += s.get("duration_seconds", 0)
            except Exception:
                pass

        hrs = total_sec / 3600 if total_sec else 1

        # Zeitspanne / Pausen / Session-Kennzahlen
        starts, ends = [], []
        for s in active_sessions:
            try:
                st = datetime.fromisoformat(s["start"])
                starts.append(st)
                ends.append(st + timedelta(seconds=s.get("duration_seconds", 0)))
            except Exception:
                pass
        first_start = min(starts).strftime("%H:%M") if starts else None
        last_end = max(ends).strftime("%H:%M") if ends else None
        span_sec = int((max(ends) - min(starts)).total_seconds()) if starts else 0
        pause_sec = int(max(0, span_sec - total_sec))
        longest_sec = max(s.get("duration_seconds", 0) for s in active_sessions)
        intens_vals = [s.get("intensity_score") for s in active_sessions
                       if s.get("intensity_score") is not None]
        avg_intensity = round(sum(intens_vals) / len(intens_vals), 1) if intens_vals else 0
        peak_hour = hourly.index(max(hourly)) if any(hourly) else None
        shots_total = sum(len(s.get("screenshot_paths") or []) for s in active_sessions)

        # Per-hour series for the stat-tile sparklines
        hs = {k: [0] * 24 for k in (
            "sessions", "focus", "switches", "keys", "clicks",
            "scrolls", "clipboard", "screenshots")}
        h_apps = [set() for _ in range(24)]
        h_projects = [set() for _ in range(24)]
        h_topics = [set() for _ in range(24)]
        h_int = [[] for _ in range(24)]
        h_dur = [[] for _ in range(24)]
        prev_app = None
        for s in active_sessions:
            try:
                h = datetime.fromisoformat(s["start"]).hour
            except Exception:
                prev_app = s.get("app_name", "")
                continue
            a = s.get("app_name", "")
            dur = s.get("duration_seconds", 0)
            hs["sessions"][h] += 1
            if dur >= 1500:
                hs["focus"][h] += dur
            hs["keys"][h] += s.get("keystrokes_total", 0)
            hs["clicks"][h] += s.get("mouse_clicks_total", 0)
            hs["scrolls"][h] += s.get("scroll_events_total", 0)
            hs["clipboard"][h] += len(s.get("clipboard_events", []))
            hs["screenshots"][h] += len(s.get("screenshot_paths") or [])
            if prev_app is not None and a != prev_app:
                hs["switches"][h] += 1
            prev_app = a
            h_apps[h].add(a)
            p = s.get("project")
            if p and p != "Other":
                h_projects[h].add(p)
            t = s.get("topic")
            if t:
                h_topics[h].add(t)
            i = s.get("intensity_score")
            if i is not None:
                h_int[h].append(i)
            h_dur[h].append(dur)
        hourly_series = {
            **hs,
            "active": [int(v) for v in hourly],
            "apps": [len(x) for x in h_apps],
            "projects": [len(x) for x in h_projects],
            "topics": [len(x) for x in h_topics],
            "intensity": [round(sum(v) / len(v), 1) if v else 0 for v in h_int],
            "avg_session": [int(sum(v) / len(v)) if v else 0 for v in h_dur],
            "longest": [int(max(v)) if v else 0 for v in h_dur],
        }

        # Topics (grouped by `topic` field, filled by topic_extractor)
        topic_list = aggregate_topics(active_sessions, top_n=12, min_sec=60)

        day_stats = {
            "total_sec": total_sec,
            "sessions": len(active_sessions),
            "focus_count": len(focus),
            "focus_sec": focus_sec,
            "switches": switches,
            "switches_ph": round(switches / hrs, 1),
            "keys": keys,
            "clicks": clicks,
            "scrolls": scrolls,
            "clipboard": clip,
            "first_start": first_start,
            "last_end": last_end,
            "span_sec": span_sec,
            "pause_sec": pause_sec,
            "avg_session_sec": int(total_sec / len(active_sessions)),
            "longest_session_sec": longest_sec,
            "avg_intensity": avg_intensity,
            "peak_hour": peak_hour,
            "keys_ph": int(keys / hrs),
            "clicks_ph": int(clicks / hrs),
            "scrolls_ph": int(scrolls / hrs),
            # ~100 px pro Scroll-Event bei 96 dpi → Meter
            "scroll_m": round(scrolls * 100 * 0.0254 / 96, 1),
            "clipboard_ph": round(clip / hrs, 1),
            "sessions_ph": round(len(active_sessions) / hrs, 1),
            "screenshots_ph": round(shots_total / hrs, 1),
            "app_count": len(app_times),
            "project_count": len(projects),
            "screenshots": shots_total,
            "hourly_series": hourly_series,
            "projects": proj_list,
            "topics": topic_list,
            "app_categories": cat_list,
            "apps": app_list,
            "hourly": hourly,
            "web_categories": build_web_category_tree(active_sessions),
        }

    # Recent Sessions (exclude inactive apps)
    recent_sess = []
    for s in reversed(active_sessions):
        try:
            t = datetime.fromisoformat(s["start"]).strftime("%H:%M")
        except Exception:
            t = "—"
        shots = [p for p in (s.get("screenshot_paths") or []) if isinstance(p, str) and p]
        first_shot_url = ""
        if shots:
            try:
                parts = shots[0].split("/")
                first_shot_url = f"/screenshots/file/{parts[-2]}/{parts[-1]}"
            except Exception:
                first_shot_url = ""
        recent_sess.append({
            "time": t,
            "app": s.get("app_name", "—"),
            "title": (s.get("window_title") or "—")[:60],
            "project": s.get("project", ""),
            "topic": s.get("topic", ""),
            "dur": s.get("duration_seconds", 0),
            "intensity": s.get("intensity_score", 0),
            "screenshots": len(shots),
            "screenshot_url": first_shot_url,
        })

    # Reports
    reports = {
        "daily": latest_report("daily"),
        "weekly": latest_report("weekly"),
        "monthly": latest_report("monthly"),
    }
    report_groups = {
        "daily": latest_report_group("daily"),
        "weekly": latest_report_group("weekly"),
        "monthly": latest_report_group("monthly"),
    }

    # Logs
    logs = log_tail(LOGS / "collector.log", 3)

    return jsonify({
        "ts": now.isoformat(),
        "today": today,
        "snap_total": snap_total,
        "interval": interval,
        "services": services,
        "live": live,
        "day": day_stats,
        "recent_sessions": recent_sess,
        "reports": reports,
        "report_groups": report_groups,
        "logs": logs,
    })


@app.route("/api/rhythm")
@app.route("/api/rhythm/<int:weeks>")
def api_rhythm(weeks=2):
    """Return heatmap data for the last N weeks.

    Rhythm days follow a 06:00 → 06:00 definition: each day spans from
    06:00 on its start date to 06:00 on the next calendar day. Returned
    ``hours`` arrays are already ordered by display position, starting at
    ``display_start`` (6) and wrapping through midnight back to 05:00.
    """
    from rhythm_heatmap import get_active_hours
    from datetime import timedelta as td

    weeks = min(weeks, 8)
    days = weeks * 7

    DAY_START = 6   # 06:00 → 06:00 day definition (first column = 06:00–07:00)
    DAY_HR_START = 6   # Tagesarbeitszeit: 06:00 … 20:00
    DAY_HR_END = 20    # Nachtarbeitszeit: 20:00 … 06:00

    def _hours_for_rhythm_day(start_date):
        """Return set of wall-clock hours active during start_date 06:00 → next 06:00."""
        active = set()
        # Part 1: start_date 06..23
        h1 = get_active_hours(SUMMARIES / "daily" / f"{start_date.strftime('%Y-%m-%d')}.md")
        for h in h1:
            if h >= DAY_START:
                active.add(h)
        # Part 2: (start_date + 1) 0..5
        next_date = start_date + td(days=1)
        h2 = get_active_hours(SUMMARIES / "daily" / f"{next_date.strftime('%Y-%m-%d')}.md")
        for h in h2:
            if h < DAY_START:
                active.add(h)
        return active

    # The rhythm day that contains "now": if now.hour >= 6, it starts today;
    # otherwise it started yesterday.
    now = datetime.now()
    if now.hour >= DAY_START:
        current_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        current_start = (now - td(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Display order: hours 06..23, then 0..5 (24 positions total)
    display_hours = list(range(DAY_START, 24)) + list(range(0, DAY_START))

    result = []
    for i in range(days - 1, -1, -1):
        start_date = current_start - td(days=i)
        hours = _hours_for_rhythm_day(start_date)
        cells = []
        for h in display_hours:
            is_day = DAY_HR_START <= h < DAY_HR_END
            if h in hours:
                cells.append("healthy" if is_day else "unhealthy")
            else:
                cells.append("missed" if is_day else "rest")
        result.append({
            "date": start_date.strftime("%Y-%m-%d"),
            "weekday": start_date.strftime("%a"),
            "weekend": start_date.weekday() >= 5,
            "today": i == 0,
            "hours": cells,
            "active": len(hours),
            "healthy": sum(1 for h in hours if DAY_HR_START <= h < DAY_HR_END),
            "unhealthy": sum(1 for h in hours if h < DAY_HR_START or h >= DAY_HR_END),
        })

    total_active = sum(d["active"] for d in result if d["active"])
    total_healthy = sum(d["healthy"] for d in result)
    total_night = sum(d["unhealthy"] for d in result)
    days_tracked = sum(1 for d in result if d["active"])

    return jsonify({
        "days": result,
        "display_hours": display_hours,
        "day_start": DAY_START,
        "healthy_start": DAY_HR_START,
        "healthy_end": DAY_HR_END,
        "stats": {
            "avg_active": round(total_active / days_tracked, 1) if days_tracked else 0,
            "healthy_pct": round(total_healthy / total_active * 100) if total_active else 0,
            "day_hours": total_healthy,
            "night_hours": total_night,
            "days_tracked": days_tracked,
            "total_days": days,
        }
    })


@app.route("/api/report/<rtype>/<name>")
def api_report(rtype, name):
    if rtype not in ("daily", "weekly", "monthly"):
        return "invalid", 400
    if ".." in name or "/" in name:
        return "invalid", 400
    path = SUMMARIES / rtype / name
    try:
        return path.read_text()
    except FileNotFoundError:
        return "not found", 404


@app.route("/api/reports/<rtype>")
def api_reports_list(rtype):
    if rtype not in ("daily", "weekly", "monthly"):
        return jsonify([]), 400
    return jsonify(all_reports(rtype))


@app.route("/api/open/<rtype>/<name>")
def api_open_file(rtype, name):
    if rtype not in ("daily", "weekly", "monthly"):
        return "invalid type", 400
    if ".." in name or "/" in name:
        return "invalid name", 400
    path = SUMMARIES / rtype / name
    if not path.exists():
        return "not found", 404
    subprocess.Popen(["open", str(path)])
    return jsonify({"ok": True, "path": str(path)})


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/explore")
@app.route("/explore/<date>")
def explore(date=None):
    return render_template_string(EXPLORE_HTML)


@app.route("/statistics")
def statistics():
    return render_template_string(STATS_HTML)


# ── API: Explore ────────────────────────────────────────────


def get_available_dates():
    dates = set()
    for f in DATA_SESS.glob("*.json"):
        dates.add(f.stem)
    for f in DATA_SNAP.glob("*.jsonl"):
        dates.add(f.stem)
    return sorted(dates)


def load_all_snapshots(date_str):
    path = DATA_SNAP / f"{date_str}.jsonl"
    result = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        result.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return result


@app.route("/api/dates")
def api_dates():
    return jsonify(get_available_dates())


@app.route("/api/sessions/<date>")
def api_sessions(date):
    sessions = load_sessions(date)
    active = [s for s in sessions if s.get("app_name") not in INACTIVE_APPS]
    cats = _load_app_categories()
    for s in active:
        s["app_category"] = classify_app(s.get("app_name", ""), cats)
    return jsonify(sanitize_for_json(active))


@app.route("/api/topics/<date>")
def api_topics(date):
    """Return aggregated topics for a single day."""
    sessions = load_sessions(date)
    active = [s for s in sessions if s.get("app_name") not in INACTIVE_APPS]
    topics = aggregate_topics(active, top_n=50, min_sec=30)
    total_with_topic = sum(1 for s in active if (s.get("topic") or "").strip())
    return jsonify({
        "date": date,
        "topics": topics,
        "sessions_total": len(active),
        "sessions_with_topic": total_with_topic,
    })


@app.route("/api/snapshots/<date>/range")
def api_snapshots_range(date):
    from datetime import timezone
    from urllib.parse import unquote
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    if not start or not end:
        return jsonify([]), 400

    # URL decoding may turn '+02:00' into ' 02:00'
    start = start.replace(" ", "+")
    end = end.replace(" ", "+")

    start_dt = datetime.fromisoformat(start).astimezone(timezone.utc)
    end_dt = datetime.fromisoformat(end).astimezone(timezone.utc)

    snaps = load_all_snapshots(date)
    result = []
    for s in snaps:
        ts = datetime.fromisoformat(s["ts"]).astimezone(timezone.utc)
        if ts < start_dt:
            continue
        if ts > end_dt:
            break
        result.append(s)
    return jsonify(result)


@app.route("/api/snapshots/<date>/timeline")
def api_snapshots_timeline(date):
    snaps = load_all_snapshots(date)
    result = []
    for s in snaps:
        inp = s.get("input", {})
        aa = s.get("active_app", {})
        result.append({
            "ts": s.get("ts"),
            "app": aa.get("name"),
            "title": aa.get("window_title"),
            "keys": inp.get("keystrokes", 0),
            "clicks": inp.get("mouse_clicks_left", 0) + inp.get("mouse_clicks_right", 0),
            "scroll": inp.get("scroll_events", 0),
            "idle_kb": inp.get("idle_seconds_keyboard", 0),
            "idle_ms": inp.get("idle_seconds_mouse", 0),
        })
    return jsonify(result)


@app.route("/api/statistics")
def api_statistics():
    """Aggregated topic×project×app triples for a date range."""
    end = request.args.get("end") or datetime.now().strftime("%Y-%m-%d")
    start = request.args.get("start")
    if not start:
        start_dt = datetime.strptime(end, "%Y-%m-%d") - timedelta(days=6)
        start = start_dt.strftime("%Y-%m-%d")

    try:
        datetime.strptime(start, "%Y-%m-%d")
        datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "invalid date format, expected YYYY-MM-DD"}), 400

    all_sessions = []
    days_with_data = 0
    for d in _dates_in_range(start, end):
        day_sessions = load_sessions(d)
        if not day_sessions:
            continue
        active = [s for s in day_sessions if s.get("app_name") not in INACTIVE_APPS]
        if active:
            days_with_data += 1
            all_sessions.extend(active)

    triples = aggregate_triples(all_sessions)
    total_sec = sum(t["sec"] for t in triples)
    total_count = sum(t["count"] for t in triples)

    return jsonify(sanitize_for_json({
        "start": start,
        "end": end,
        "days_with_data": days_with_data,
        "total_sec": total_sec,
        "total_sessions": total_count,
        "triples": triples,
    }))


# ── Screenshots ─────────────────────────────────────────────
import re as _re

# Accepted screenshot formats: PNG is what the collector writes; JPG/JPEG
# is what ``wt compress`` produces. The dashboard must serve both so old
# (compressed) days stay visible alongside today's (raw) captures.
_IMG_EXTS = ("png", "jpg", "jpeg")
_IMG_GLOBS = tuple(f"*.{ext}" for ext in _IMG_EXTS)
_MIME_FOR_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}

_SAFE_DATE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_IMG = _re.compile(r"^[A-Za-z0-9_\-]+\.(?:png|jpe?g)$", _re.IGNORECASE)


def _iter_screenshots(folder):
    """Yield all image files in ``folder`` across supported extensions.

    macOS creates ``._<name>`` AppleDouble shadows on non-HFS+ volumes
    (e.g. external drives formatted exFAT). These are binary resource
    forks, not real images — skip them so the UI stays clean.
    """
    for pat in _IMG_GLOBS:
        for f in folder.glob(pat):
            if f.name.startswith("._"):
                continue
            yield f


def _folder_has_screenshots(folder) -> bool:
    return any(True for _ in _iter_screenshots(folder))


def _mimetype_for(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower()
    return _MIME_FOR_EXT.get(ext, "application/octet-stream")


def _screenshot_dates() -> list[str]:
    if not DATA_SCREENSHOTS.exists():
        return []
    out = []
    for d in DATA_SCREENSHOTS.iterdir():
        if d.is_dir() and _SAFE_DATE.match(d.name) and _folder_has_screenshots(d):
            out.append(d.name)
    return sorted(out, reverse=True)


def _parse_shot_filename_to_iso(filename: str) -> "str | None":
    """Convert ``YYYYMMDDTHHMMSS.<ext>`` to an ISO timestamp string."""
    stem = filename.rsplit(".", 1)[0]
    try:
        return datetime.strptime(stem, "%Y%m%dT%H%M%S").isoformat()
    except ValueError:
        return None


def _build_path_to_session_map(sessions):
    """Map absolute screenshot paths to the owning session dict."""
    m = {}
    for s in sessions:
        for p in s.get("screenshot_paths") or []:
            if isinstance(p, str) and p:
                m[p] = s
    return m


@app.route("/api/screenshots/dates")
def api_screenshot_dates():
    return jsonify(_screenshot_dates())


@app.route("/api/screenshots/<date>")
def api_screenshots_for_date(date):
    if not _SAFE_DATE.match(date):
        return jsonify({"error": "invalid date"}), 400
    folder = DATA_SCREENSHOTS / date
    if not folder.is_dir():
        return jsonify({"date": date, "items": []})

    sessions = load_sessions(date)
    path_to_session = _build_path_to_session_map(sessions)

    items = []
    for f in sorted(_iter_screenshots(folder)):
        abs_path = str(f)
        sess = path_to_session.get(abs_path)
        ts_iso = _parse_shot_filename_to_iso(f.name)
        item = {
            "filename": f.name,
            "ts": ts_iso,
            "url": f"/screenshots/file/{date}/{f.name}",
            "size_bytes": f.stat().st_size,
            "session_app": (sess or {}).get("app_name") if sess else None,
            "session_project": (sess or {}).get("project") if sess else None,
            "session_topic": (sess or {}).get("topic") if sess else None,
            "session_motivation": (sess or {}).get("motivation_message") if sess else None,
            "session_start": (sess or {}).get("start") if sess else None,
            "session_end": (sess or {}).get("end") if sess else None,
        }
        items.append(item)

    items.sort(key=lambda it: it["ts"] or it["filename"], reverse=True)
    return jsonify({"date": date, "items": items})


@app.route("/screenshots/file/<date>/<filename>")
def screenshot_file(date, filename):
    if not _SAFE_DATE.match(date) or not _SAFE_IMG.match(filename):
        abort(404)
    folder = DATA_SCREENSHOTS / date
    if not (folder / filename).is_file():
        abort(404)
    return send_from_directory(folder, filename, mimetype=_mimetype_for(filename))


@app.route("/screenshots")
def screenshots_page():
    return render_template_string(SCREENSHOTS_HTML)


# ── Docs (static, served from ~/WorkTracker/docs) ────────────────────────────
DOCS_DIR = BASE / "docs"


@app.route("/docs")
@app.route("/docs/")
def docs_index():
    if not (DOCS_DIR / "index.html").exists():
        abort(404)
    return send_from_directory(str(DOCS_DIR), "index.html")


@app.route("/docs/<path:filename>")
def docs_file(filename):
    if ".." in filename:
        abort(400)
    return send_from_directory(str(DOCS_DIR), filename)


# ── HTML ─────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkTracker Dashboard</title>
<style>
:root {
  /* Palette mirrors docs/index.html — acid-green accent, slate-blue cyan */
  --bg: #0c1116; --bg2: #141a20; --bg3: #1b222a;
  --fg: #cfd7e0; --fg2: #8a95a3; --fg3: #484f58;
  --cyan: #4fc3d8; --green: #6fe28a; --yellow: #d29922; --acid: #d4f500;
  --red: #ff4d4f; --purple: #b392ff; --blue: #4fc3d8;
  --border: #2b3642; --white: #f4f8ff;
  --orange: #d18616;
  --card-top: #161e26; --card-hover: #3a4754;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  background: var(--bg); color: var(--fg);
  font-size: 13px; line-height: 1.5;
  padding: 16px clamp(16px, 2.5vw, 36px) 48px; max-width: 1760px; margin: 0 auto;
}
h1 { font-size: 18px; color: var(--cyan); font-weight: 600; }
h2 {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px;
  color: var(--fg2); margin-bottom: 8px; padding-bottom: 5px;
  border-bottom: 1px solid var(--border);
}
h2::before {
  content: ''; display: inline-block; width: 7px; height: 7px;
  background: var(--acid); border-radius: 2px; margin-right: 8px;
}
.header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 0 12px; border-bottom: 1px solid var(--bg3); margin-bottom: 16px;
}
.header-right { color: var(--fg2); font-size: 12px; }
.header-right .dot { color: var(--green); font-size: 16px; vertical-align: middle; }
.grid {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(min(440px, 100%), 1fr));
}
.card {
  background: linear-gradient(180deg, var(--card-top) 0%, var(--bg2) 55%);
  border: 1px solid var(--border);
  border-radius: 14px; padding: 16px;
  box-shadow: 0 1px 2px rgba(0,0,0,.25), 0 14px 30px -24px rgba(0,0,0,.6);
  transition: border-color 0.15s, box-shadow 0.2s, transform 0.15s;
}
.card:hover { border-color: var(--card-hover);
  box-shadow: 0 2px 4px rgba(0,0,0,.25), 0 18px 36px -22px rgba(0,0,0,.65); }
.card.wide { grid-column: 1 / -1; }
.pill {
  display: inline-block; padding: 2px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 600;
}
.pill.ok { background: rgba(111,226,138,.14); color: var(--green); }
.pill.warn { background: rgba(210,153,34,.16); color: var(--yellow); }
.pill.err { background: rgba(255,77,79,.14); color: var(--red); }
.pill.idle { background: rgba(79,195,216,.14); color: var(--blue); }

/* Live */
.live-app { font-size: 20px; font-weight: 700; color: var(--fg); }
.live-window { color: var(--fg2); font-size: 13px; margin-top: 2px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.live-media { color: var(--purple); font-size: 12px; margin-top: 6px; }

/* Meters */
.meter-row { display: flex; gap: 24px; margin-top: 10px; flex-wrap: wrap; }
.meter { flex: 1; min-width: 100px; }
.meter-label { font-size: 11px; color: var(--fg2); margin-bottom: 3px; }
.meter-bar {
  height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden;
}
.meter-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; }
.meter-val { font-size: 12px; color: var(--fg); margin-top: 2px; }

/* Services */
.svc {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 8px; margin: 0 -8px; font-size: 12px;
  border-radius: 6px; transition: background 0.15s;
}
.svc:hover { background: var(--bg3); }
.svc-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.svc-dot.on {
  background: var(--green); box-shadow: 0 0 0 0 var(--green);
  animation: svc-pulse 2s infinite;
}
@keyframes svc-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(63,185,80,0.5); }
  70%  { box-shadow: 0 0 0 5px rgba(63,185,80,0); }
  100% { box-shadow: 0 0 0 0 rgba(63,185,80,0); }
}
.svc-dot.sched { background: var(--blue); }
.svc-dot.off { background: var(--fg3); }
.svc-name { color: var(--fg); font-weight: 500; min-width: 104px; flex-shrink: 0; }
.svc-info {
  color: var(--fg2); font-size: 11px;
  font-family: 'SF Mono', 'Fira Code', monospace;
}
.svc-info .exit-ok { color: var(--green); }
.svc-info .exit-err { color: var(--red); }
/* Per-service start/stop/restart controls — only sensible actions are
   rendered; they fade in on row hover to keep the panel calm. */
.svc-actions { margin-left: auto; display: inline-flex; gap: 5px; flex-shrink: 0;
  opacity: 0; transition: opacity .12s; }
.svc:hover .svc-actions, .svc.svc-working .svc-actions { opacity: 1; }
.svc-btn {
  font-family: inherit; font-size: 10px; letter-spacing: .4px; text-transform: uppercase;
  padding: 3px 8px; border-radius: 5px; cursor: pointer;
  background: var(--bg3); color: var(--fg2); border: 1px solid var(--border);
  transition: color .12s, border-color .12s, background .12s;
}
.svc-btn:hover { color: var(--fg); border-color: var(--cyan); background: var(--bg2); }
.svc-start:hover   { color: var(--green); border-color: var(--green); }
.svc-restart:hover { color: var(--acid);  border-color: var(--acid); }
.svc-stop:hover    { color: var(--red);   border-color: var(--red); }
.svc-btn:disabled { opacity: .4; cursor: default; }
.svc.svc-working { opacity: .65; }

/* Stats */
.stats { display: flex; flex-wrap: wrap; gap: 6px 20px; }
.stat { text-align: center; min-width: 80px; }
.stat-val { font-size: 22px; font-weight: 700; color: var(--fg); }
.stat-label { font-size: 10px; color: var(--fg2); text-transform: uppercase; }

/* Day stats — tile row */
#day-stats {
  display: grid; gap: 1px;
  grid-template-columns: repeat(auto-fit, minmax(155px, 1fr));
  background: var(--bg3); border: 1px solid var(--bg3);
  border-radius: 8px; overflow: hidden; margin-top: 10px;
}
#day-stats .stat {
  background: var(--bg2); padding: 12px 8px 10px; min-width: 0;
  transition: background 0.15s;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Roboto, sans-serif;
}
#day-stats .stat:hover { background: var(--bg3); }
#day-stats .stat-val {
  font-size: 20px; font-weight: 700; color: var(--fg);
  font-variant-numeric: tabular-nums; letter-spacing: -0.3px;
  white-space: nowrap;
}
#day-stats .stat-label {
  font-size: 12px; color: var(--fg2); letter-spacing: 1px;
  text-transform: uppercase; margin-top: 4px; font-weight: 700;
}
#day-stats .stat-spark, #day-stats .stat-spark-empty {
  display: block; width: 100%; height: 14px; margin-top: 6px;
}
#day-stats .stat-spark rect { fill: var(--cyan); opacity: 0.65; }
#day-stats .stat-spark rect.spark-zero { fill: var(--bg3); opacity: 1; }
#day-stats .stat-spark rect.spark-hi { fill: var(--green); opacity: 1; }
#day-stats .stat:hover .stat-spark rect:not(.spark-zero) { opacity: 0.9; }
#day-stats .stat-sub {
  display: block; font-size: 11px; font-weight: 500;
  color: var(--fg3); letter-spacing: 0; margin-top: 1px;
}

/* Gauges Row — 3 SVG ring gauges at the top of Daily Overview */
.gauges-row {
  display: flex; justify-content: space-around; align-items: center;
  gap: 14px; padding: 10px 6px 18px; margin-bottom: 10px;
  border-bottom: 1px solid var(--bg3);
}
.gauge {
  position: relative; display: flex; flex-direction: column; align-items: center;
  justify-content: center;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Roboto, sans-serif;
}
.gauge-svg { display: block; }
.gauge-value {
  position: absolute; top: 0; left: 0; width: 100%; height: 100%;
  display: flex; align-items: center; justify-content: center;
  font-size: 15px; font-weight: 700; color: var(--fg);
  pointer-events: none;
}
.gauge.gauge-hero .gauge-value {
  flex-direction: column; line-height: 1.05; font-size: 15px;
}
.gauge-value-min { font-size: 11px; font-weight: 600; color: var(--fg2); }
.gauge-label {
  margin-top: 6px; font-size: 10px; color: var(--fg2);
  text-transform: uppercase; letter-spacing: 0.5px;
}
.gauge-sub {
  margin-top: 2px; font-size: 10px; color: var(--fg3);
}
/* Combined Work+Pause donut */
.gauge-combo-center {
  position: absolute; top: 0; left: 0; width: 100%; height: 100%;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  line-height: 1.05; pointer-events: none;
}
.gauge-combo-center .cc-main { font-size: 22px; font-weight: 700; }
.gauge-combo-center .cc-focus { font-size: 22px; font-weight: 900; margin-top: 1px; }
.gauge-combo-center .cc-sub { font-size: 13px; font-weight: 600; margin-top: 1px; }
.gauge-combo-legend {
  margin-top: 6px; display: flex; flex-direction: column; gap: 3px;
  font-size: 11px; color: var(--fg2);
}
.gauge-combo-legend span { display: flex; align-items: center; gap: 6px; }
.gauge-combo-legend i {
  width: 8px; height: 8px; border-radius: 2px; flex: none;
}

/* Project bars */
.proj-row { display: flex; align-items: center; gap: 8px; padding: 4px 0;
  border-bottom: 1px solid var(--bg3); font-size: 12px; }
.proj-row:last-child { border-bottom: none; }
.dist-toggle { display: block; text-align: center; padding: 6px 0; margin-top: 4px; color: var(--cyan); font-size: 12px; text-decoration: none; cursor: pointer; }
.dist-toggle:hover { text-decoration: underline; }
.dist-pagination { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--bg3); }
.dist-page-btn { font-size: 11px; padding: 3px 8px; border-radius: 4px; background: var(--bg3); color: var(--fg2); text-decoration: none; cursor: pointer; }
.dist-page-btn:hover { background: var(--cyan); color: var(--bg); }
.dist-page-btn.active { background: var(--cyan); color: var(--bg); }
.proj-name { width: 140px; flex-shrink: 0; color: var(--fg); font-weight: 500;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cat-toggle { cursor: pointer; }
.cat-toggle:hover { background: var(--bg3); border-radius: 4px; }
.cat-arrow { width: 14px; flex-shrink: 0; color: var(--fg3); font-size: 10px; text-align: center; transition: transform 0.15s; display: inline-block; }
.cat-arrow.open { transform: rotate(90deg); }
.cat-arrow-spacer { width: 14px; flex-shrink: 0; }
.sub-row { padding-left: 4px; border-bottom-color: transparent; }
.sub-row .proj-name { color: var(--fg2); font-weight: 400; font-size: 11px; }
.sub-row .proj-pct, .sub-row .proj-time { font-size: 11px; opacity: 0.7; }
.cat-subs { border-left: 2px solid var(--bg3); margin-left: 6px; padding-left: 2px; margin-bottom: 4px; }
.wcat-subs { border-left: 2px solid var(--bg3); margin-left: 6px; padding-left: 2px; margin-bottom: 2px; }
.wcat-domain { padding-left: 4px; }
.wcat-domain .proj-name { color: var(--fg3); font-weight: 400; font-size: 11px; }
.wcat-domain .proj-pct, .wcat-domain .proj-time { font-size: 11px; opacity: 0.5; }
.proj-bar-wrap { flex: 1; height: 8px; background: var(--bg3); border-radius: 4px; overflow: hidden; }
.proj-bar { height: 100%; border-radius: 4px; transition: width 0.5s; }
.proj-pct { width: 40px; text-align: right; color: var(--fg2); }
.proj-time { width: 55px; text-align: right; color: var(--fg2); }
.proj-int { width: 30px; text-align: right; color: var(--yellow); }

/* Topics (styled similar to projects but with cyan accent) */
.topic-row { display: flex; align-items: center; gap: 8px; padding: 5px 0;
  border-bottom: 1px solid var(--bg3); font-size: 12px; }
.topic-row:last-child { border-bottom: none; }
/* Rows that carry a topic_long get a help cursor + dotted underline hint. */
.topic-row.has-long { cursor: help; }
.topic-row.has-long .topic-name {
  text-decoration: underline dotted var(--fg3); text-underline-offset: 3px; }
.topic-name { flex: 1; color: var(--fg); font-weight: 500; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.topic-proj { color: var(--cyan); font-size: 11px; font-weight: 500;
  padding: 2px 7px; border: 1px solid var(--bg3); border-radius: 10px;
  max-width: 130px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.topic-bar-wrap { width: 90px; flex-shrink: 0; height: 6px;
  background: var(--bg3); border-radius: 3px; overflow: hidden; }
.topic-bar { height: 100%; background: var(--cyan); border-radius: 3px;
  transition: width 0.4s; }
.topic-time { width: 55px; text-align: right; color: var(--yellow); font-size: 11px; }
.topic-sessions { width: 28px; text-align: right; color: var(--fg3); font-size: 11px; }
.card-meta { font-size: 10px; color: var(--fg3); font-weight: 400;
  margin-left: 6px; text-transform: none; }

/* Sessions */
.sess-row { display: flex; gap: 8px; padding: 5px 0;
  border-bottom: 1px solid var(--bg3); font-size: 12px; align-items: center; }
.sess-row:last-child { border-bottom: none; }
.sess-time { width: 42px; color: var(--fg2); flex-shrink: 0; }
.sess-app { width: 110px; color: var(--fg); font-weight: 500; flex-shrink: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sess-title { flex: 1; color: var(--fg2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sess-proj { width: 90px; flex-shrink: 0; text-align: right; color: var(--blue); font-size: 11px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sess-topic { width: 140px; flex-shrink: 0; color: var(--cyan); font-size: 11px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-style: italic; }
.sess-topic:empty { display: none; }
.sess-dur-wrap { flex: 0.6; display: flex; align-items: center; gap: 6px; min-width: 100px; }
.sess-dur { flex-shrink: 0; text-align: right; color: var(--yellow); white-space: nowrap; }
.sess-dur-bar { flex: 1; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.sess-dur-fill { height: 100%; border-radius: 3px; background: #888; }
.sess-int-wrap { width: 36px; flex-shrink: 0; display: flex; align-items: flex-end; gap: 2px; height: 14px; }
.sess-int-seg { width: 4px; border-radius: 1px; }
.sess-scr { width: 44px; flex-shrink: 0; text-align: right; color: var(--fg2); font-size: 11px;
  text-decoration: none; white-space: nowrap; }
.sess-scr:hover { color: var(--cyan); }
.sess-scr-empty { width: 44px; flex-shrink: 0; }

/* Chart */
.chart-wrap { height: 120px; display: flex; align-items: flex-end; gap: 2px; padding-top: 8px; }
.chart-bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; }
.chart-bar {
  width: 100%; min-width: 8px; background: var(--cyan); border-radius: 3px 3px 0 0;
  transition: height 0.5s; opacity: 0.7;
}
.chart-bar.now { opacity: 1; background: var(--green); }
.chart-lbl { font-size: 9px; color: var(--fg3); margin-top: 2px; height: 11px; line-height: 11px; }

/* Reports */
.rpt-group { margin-bottom: 12px; }
.rpt-group-label {
  display: inline-block; font-size: 9px; text-transform: uppercase;
  letter-spacing: 1px; font-weight: 700; color: var(--fg3);
  background: var(--bg3); padding: 2px 7px; border-radius: 4px;
  margin-bottom: 5px;
}
.rpt {
  display: flex; align-items: center; gap: 9px;
  padding: 5px 8px; margin: 0 -8px; font-size: 12px;
  border-radius: 6px; transition: background 0.15s;
}
.rpt:hover { background: var(--bg3); }
.rpt-name { color: var(--cyan); cursor: pointer; text-decoration: none; font-weight: 500; }
.rpt-name:hover { text-decoration: underline; }
.rpt-size { color: var(--fg3); font-size: 10px; font-family: 'SF Mono','Fira Code',monospace; }
.rpt-age { color: var(--fg3); font-size: 11px; margin-left: auto; }
.rpt-view {
  cursor: pointer; color: var(--fg3); font-size: 12px;
  width: 16px; text-align: center; flex-shrink: 0;
  opacity: 0.55; transition: opacity 0.15s, transform 0.1s;
}
.rpt:hover .rpt-view { opacity: 1; }
.rpt-view:hover { color: var(--cyan); transform: scale(1.15); }
.rpt-more { color: var(--fg3); font-size: 11px; cursor: pointer; margin-left: auto; }
.rpt-more:hover { color: var(--cyan); }
.rpt-list { margin-top: 4px; padding-left: 75px; }
.rpt-list-item { padding: 2px 0; font-size: 11px; }
.rpt-list-item a { color: var(--fg2); cursor: pointer; text-decoration: none; }
.rpt-list-item a:hover { color: var(--cyan); text-decoration: underline; }

/* Report modal */
.modal-overlay {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.7); z-index: 100;
  justify-content: center; align-items: center;
}
.modal-overlay.show { display: flex; }
.modal {
  background: var(--bg2); border: 1px solid var(--bg3);
  border-radius: 10px; width: 90%; max-width: 900px;
  max-height: 85vh; display: flex; flex-direction: column;
}
.modal-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 18px; border-bottom: 1px solid var(--bg3);
}
.modal-title { font-size: 14px; font-weight: 600; color: var(--cyan); }
.modal-close {
  background: none; border: none; color: var(--fg2);
  font-size: 20px; cursor: pointer; padding: 0 4px;
}
.modal-close:hover { color: var(--fg); }
.modal-body {
  padding: 18px; overflow-y: auto; flex: 1;
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 12px; line-height: 1.6; color: var(--fg);
  white-space: pre-wrap; word-wrap: break-word;
}

/* Logs */
.log-line {
  font-size: 11px; color: var(--fg2); padding: 2px 8px; margin: 0 -8px;
  font-family: 'SF Mono', 'Fira Code', monospace; border-radius: 4px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.log-line:hover { background: var(--bg3); white-space: normal; word-break: break-word; }
.log-ts { color: var(--fg3); }
.log-lvl { font-weight: 700; margin: 0 4px; }
.log-lvl.info { color: var(--blue); }
.log-lvl.warn { color: var(--yellow); }
.log-lvl.error { color: var(--red); }

/* System */
.sys-row { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--fg2); margin-top: 6px; }

/* Rhythm Heatmap */
.heatmap { margin-top: 6px; }
.heatmap-row { display: flex; align-items: center; gap: 0; margin-bottom: 1px; }
.heatmap-label {
  width: 70px; flex-shrink: 0; font-size: 11px; color: var(--fg2);
  text-align: right; padding-right: 8px;
}
.heatmap-label.today { color: var(--fg); font-weight: 600; }
.heatmap-label.weekend { color: var(--yellow); }
.heatmap-cell {
  width: 12px; height: 12px; margin: 0.5px; border-radius: 2px;
  transition: opacity 0.3s;
}
.heatmap-cell.rest { background: var(--bg3); }
.heatmap-cell.missed { background: var(--bg3); }
.heatmap-cell.healthy { background: #6cb6ff; opacity: 0.9; }
.heatmap-cell.unhealthy { background: #1f3a8a; opacity: 0.9; }
.heatmap-cell:hover { opacity: 1; outline: 1px solid var(--fg2); }
.heatmap-hours { display: flex; margin-left: 70px; margin-bottom: 4px; }
.heatmap-hours span { width: 13px; font-size: 8px; color: var(--fg3); text-align: left; overflow: visible; }
.heatmap-legend {
  display: flex; gap: 16px; margin-top: 8px; margin-left: 78px; font-size: 11px; color: var(--fg2);
}
.heatmap-legend-dot {
  display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  vertical-align: middle; margin-right: 4px;
}
.heatmap-stats {
  display: flex; gap: 20px; margin-top: 10px; margin-left: 78px; font-size: 12px;
  align-items: center;
}
.heatmap-stats .stat-val { font-size: 18px; }
.heatmap-donut { display: flex; align-items: center; gap: 10px; }
.heatmap-donut svg { display: block; }
.heatmap-donut-legend { font-size: 11px; line-height: 1.5; }
.heatmap-donut-legend .dn-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 2px;
  margin-right: 5px; vertical-align: middle;
}
.heatmap-donut-legend .dn-pct { color: var(--fg); font-weight: 600; }
.heatmap-donut-legend .dn-name { color: var(--fg2); }
.heatmap-sep { border: none; border-top: 1px solid var(--bg3); margin: 2px 0 2px 78px; }
.heatmap-day-total {
  width: 30px; flex-shrink: 0; font-size: 10px; color: var(--fg3);
  text-align: right; padding-left: 4px;
}

/* Idle overlay */
.idle-banner {
  background: rgba(79,195,216,.10); border: 1px solid var(--blue); border-radius: 6px;
  padding: 6px 12px; margin-top: 8px; color: var(--blue); font-size: 12px;
  display: none;
}
.idle-banner.show { display: block; }
</style>
</head>
<body>

@@NAV:dashboard@@

<div class="grid">

  <!-- Daily Overview — Hero-Karte zuerst: wichtigste Zusammenfassung oben -->
  <div class="card wide">
    <h2 id="day-title">Today</h2>
    <div class="gauges-row" id="gauges-row" style="display:none"></div>
    <div class="stats" id="day-stats"><div class="wt-loading"><div class="wt-spinner"></div><span class="wt-load-label">Dashboard laden…</span></div></div>
  </div>

  <!-- Live -->
  <div class="card">
    <h2>Live</h2>
    <div class="live-app" id="live-app">—</div>
    <div class="live-window" id="live-window">—</div>
    <div class="live-media" id="live-media"></div>
    <div class="idle-banner" id="idle-banner">Idle</div>
    <div class="meter-row">
      <div class="meter">
        <div class="meter-label">Keys/min</div>
        <div class="meter-bar"><div class="meter-fill" id="m-keys" style="width:0;background:var(--green)"></div></div>
        <div class="meter-val" id="v-keys">0</div>
      </div>
      <div class="meter">
        <div class="meter-label">Clicks/min</div>
        <div class="meter-bar"><div class="meter-fill" id="m-clicks" style="width:0;background:var(--cyan)"></div></div>
        <div class="meter-val" id="v-clicks">0</div>
      </div>
      <div class="meter">
        <div class="meter-label">Scroll/min</div>
        <div class="meter-bar"><div class="meter-fill" id="m-scroll" style="width:0;background:var(--purple)"></div></div>
        <div class="meter-val" id="v-scroll">0</div>
      </div>
    </div>
    <div class="sys-row" id="sys-info"></div>
  </div>

  <!-- Services -->
  <div class="card">
    <h2>Services</h2>
    <div id="services"></div>
    <h2 style="margin-top:14px">Reports</h2>
    <div id="reports"></div>
    <h2 style="margin-top:14px">Logs</h2>
    <div id="logs"></div>
  </div>

  <!-- App Categories -->
  <div class="card">
    <h2>App Categories</h2>
    <div id="app-categories"></div>
  </div>

  <!-- Web Categories -->
  <div class="card">
    <h2>Web Categories</h2>
    <div id="web-categories"></div>
  </div>

  <!-- Projects -->
  <div class="card">
    <h2>Projects</h2>
    <div id="projects"></div>
  </div>

  <!-- Topics -->
  <div class="card">
    <h2>Topics <span id="topics-meta" class="card-meta"></span></h2>
    <div id="topics"></div>
  </div>

  <!-- Hourly -->
  <div class="card">
    <h2>Activity per Hour</h2>
    <div class="chart-wrap" id="hourly-chart"></div>
  </div>

  <!-- Rhythm Heatmap -->
  <div class="card wide">
    <h2>Rhythm Heatmap</h2>
    <div class="heatmap" id="rhythm-heatmap"></div>
  </div>

  <!-- Apps -->
  <div class="card">
    <h2>Apps</h2>
    <div id="apps"></div>
  </div>

  <!-- Sessions -->
  <div class="card">
    <h2>Recent Sessions</h2>
    <div id="sessions"></div>
  </div>

</div>

<!-- Report Modal -->
<div class="modal-overlay" id="report-modal">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title" id="modal-title">Report</span>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modal-body"><div class="wt-loading"><div class="wt-spinner"></div><span class="wt-load-label">Report laden…</span></div></div>
  </div>
</div>

<script>
const REFRESH = 5000;
const expandedCats = new Set(JSON.parse(localStorage.getItem('wt_expanded_cats') || '[]'));
function toggleCat(name, id) {
  if (expandedCats.has(name)) expandedCats.delete(name); else expandedCats.add(name);
  localStorage.setItem('wt_expanded_cats', JSON.stringify([...expandedCats]));
  const el = document.getElementById(id);
  const arrow = el.previousElementSibling.querySelector('.cat-arrow');
  const open = expandedCats.has(name);
  el.style.display = open ? 'block' : 'none';
  if (arrow) arrow.classList.toggle('open', open);
}
const expandedWebCats = new Set(JSON.parse(localStorage.getItem('wt_expanded_wcats') || '[]'));
function toggleWebCat(name, id) {
  if (expandedWebCats.has(name)) expandedWebCats.delete(name); else expandedWebCats.add(name);
  localStorage.setItem('wt_expanded_wcats', JSON.stringify([...expandedWebCats]));
  const el = document.getElementById(id);
  const arrow = el.previousElementSibling.querySelector('.cat-arrow');
  const open = expandedWebCats.has(name);
  el.style.display = open ? 'block' : 'none';
  if (arrow) arrow.classList.toggle('open', open);
}
const PROJ_COLORS = [
  '#58a6ff','#3fb950','#d29922','#bc8cff','#f85149',
  '#d18616','#388bfd','#79c0ff','#56d364','#e3b341'
];

function fmt(sec) {
  if (!sec || sec < 0) return '—';
  sec = Math.round(sec);
  if (sec < 60) return sec + 's';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h > 0 ? h + 'h ' + String(m).padStart(2, '0') + 'm' : m + 'm';
}

function fmtUptime(sec) {
  if (!sec || sec <= 0) return '—';
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (d >= 1) return d + 'd ' + h + 'h';
  if (h >= 1) return h + 'h ' + String(m).padStart(2, '0') + 'm';
  if (m >= 1) return m + 'm ' + String(s).padStart(2, '0') + 's';
  return s + 's';
}

// Scroll distance in meters → "3.8 km (≈ 36× Fußballfeld)"
function scrollDist(m) {
  if (!m) return '–';
  const units = [
    [42195, 'Marathons'],
    [8849, '× Mount Everest'],
    [3798, '× Großglockner'],
    [400, 'Stadionrunden'],
    [105, '× Fußballfeld'],
    [50, '× Schwimmbecken'],
    [137, '× Stephansdom'],
    [1.8, '× Körpergröße'],
    [1, '× Meterstab'],
  ].sort((a, b) => b[0] - a[0]);
  const val = m >= 1000 ? (m / 1000).toFixed(1) + ' km' : Math.round(m) + ' m';
  const u = units.find(u => m / u[0] >= 1);
  if (!u) return val;
  const n = m / u[0];
  return val + ' (≈ ' + (n >= 10 ? Math.round(n) : n.toFixed(1)) + ' ' + u[1] + ')';
}

// Mini bar sparkline over a 24-value hourly series; hi = index to highlight
function spark(arr, hi) {
  if (!arr || !arr.some(v => v > 0)) return '<div class="stat-spark-empty"></div>';
  const max = Math.max(...arr);
  const w = 3, gap = 1, H = 14;
  const W = arr.length * (w + gap) - gap;
  let bars = '';
  arr.forEach((v, i) => {
    const h = v > 0 ? Math.max(1.5, v / max * H) : 1;
    const cls = i === hi ? ' class="spark-hi"' : (v > 0 ? '' : ' class="spark-zero"');
    bars += '<rect' + cls + ' x="' + (i * (w + gap)) + '" y="' + (H - h).toFixed(1)
          + '" width="' + w + '" height="' + h.toFixed(1) + '" rx="0.8"/>';
  });
  return '<svg class="stat-spark" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' + bars + '</svg>';
}

// SVG ring gauge: { label, value, pct (0..1), color, hero?, sub? }
function gaugeHtml(g) {
  const hero = !!g.hero;
  const size = hero ? 104 : 64;
  const stroke = hero ? 7 : 5;
  const r = (size - stroke) / 2;
  const cx = size / 2, cy = size / 2;
  const circumference = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(1, g.pct || 0));
  const offset = circumference * (1 - pct);
  const color = g.color || 'var(--cyan)';
  // Rotate -90° so progress starts at the top.
  const svg = '<svg class="gauge-svg" width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '">'
    + '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" stroke="var(--bg3)" stroke-width="' + stroke + '" fill="none" />'
    + '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" stroke="' + color + '" stroke-width="' + stroke + '"'
    + ' fill="none" stroke-linecap="round" transform="rotate(-90 ' + cx + ' ' + cy + ')"'
    + ' stroke-dasharray="' + circumference + '" stroke-dashoffset="' + offset + '" />'
    + '</svg>';
  const valueStr = (g.value == null || g.value === '') ? '—' : String(g.value);
  // Hero: split "18h 35m" so the minutes sit on their own, smaller line.
  let valueHtml;
  if (hero && valueStr.indexOf(' ') !== -1) {
    const parts = valueStr.split(' ');
    valueHtml = esc(parts[0]) + '<span class="gauge-value-min">' + esc(parts.slice(1).join(' ')) + '</span>';
  } else {
    valueHtml = esc(valueStr);
  }
  return '<div class="gauge ' + (hero ? 'gauge-hero' : '') + '">'
    + '<div style="position:relative;width:' + size + 'px;height:' + size + 'px">'
    + svg
    + '<div class="gauge-value">' + valueHtml + '</div>'
    + '</div>'
    + '<div class="gauge-label">' + esc(g.label || '') + '</div>'
    + (g.sub ? '<div class="gauge-sub">' + esc(g.sub) + '</div>' : '')
    + '</div>';
}

// Combined day donut over the full 24h. Segments (focus / work-without-focus /
// pause) plus the idle remainder sum to 24h. The focus part is drawn as a
// thicker, brighter arc. Center stacks the day shares; durations are below.
function comboGaugeHtml(workSec, pauseSec, focusSec) {
  const WORK = 'var(--cyan)', PAUSE = 'var(--orange)', FOCUS = 'var(--green)';
  const DAY = 24 * 3600;
  workSec = workSec || 0; pauseSec = pauseSec || 0; focusSec = Math.min(focusSec || 0, workSec);
  const workOnly = Math.max(0, workSec - focusSec);
  const focusFrac = focusSec / DAY;
  const workFrac = workOnly / DAY;
  const pauseFrac = pauseSec / DAY;
  const workPct = Math.round(workFrac * 100);
  const focusPct = Math.round(focusFrac * 100);
  const pausePct = Math.round(pauseFrac * 100);

  const size = 148, stroke = 9, focusStroke = 15;
  const r = (size - focusStroke) / 2;
  const cx = size / 2, cy = size / 2;
  const circ = 2 * Math.PI * r;
  const arc = (frac, start, color, sw, cap) =>
    '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none"'
    + ' stroke="' + color + '" stroke-width="' + sw + '"'
    + ' stroke-linecap="' + cap + '"'
    + ' stroke-dasharray="' + (frac * circ) + ' ' + circ + '"'
    + ' stroke-dashoffset="' + (-start * circ) + '"'
    + ' transform="rotate(-90 ' + cx + ' ' + cy + ')" />';

  // Order around the ring: focus, then remaining work, then pause.
  let svg = '<svg class="gauge-svg" width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '">'
    + '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" stroke="var(--bg3)" stroke-width="' + stroke + '" fill="none" />';
  if (workFrac > 0) svg += arc(workFrac, focusFrac, WORK, stroke, 'butt');
  if (pauseFrac > 0) svg += arc(pauseFrac, focusFrac + workFrac, PAUSE, stroke, 'butt');
  if (focusFrac > 0) svg += arc(focusFrac, 0, FOCUS, focusStroke, 'round');
  svg += '</svg>';

  return '<div class="gauge gauge-combo">'
    + '<div style="position:relative;width:' + size + 'px;height:' + size + 'px">'
    + svg
    + '<div class="gauge-combo-center">'
    +   '<span class="cc-main" style="color:' + WORK + '">' + workPct + '%</span>'
    +   '<span class="cc-focus" style="color:' + FOCUS + '">' + focusPct + '%</span>'
    +   '<span class="cc-sub" style="color:' + PAUSE + '">' + pausePct + '%</span>'
    + '</div>'
    + '</div>'
    + '<div class="gauge-label">Arbeit &amp; Pause</div>'
    + '<div class="gauge-combo-legend">'
    +   '<span><i style="background:' + WORK + '"></i>Arbeit ' + esc(fmt(workSec)) + '</span>'
    +   '<span><i style="background:' + FOCUS + '"></i>Fokus ' + esc(fmt(focusSec || 0)) + '</span>'
    +   '<span><i style="background:' + PAUSE + '"></i>Pause ' + esc(fmt(pauseSec || 0)) + '</span>'
    + '</div>'
    + '</div>';
}

function fmtAge(mtime) {
  if (!mtime) return '';
  const sec = Math.floor(Date.now() / 1000 - mtime);
  if (sec < 60) return sec + 's ago';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
  if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
  return Math.floor(sec / 86400) + 'd ago';
}

function $(id) { return document.getElementById(id); }

async function openReport(type, name) {
  const modal = $('report-modal');
  $('modal-title').textContent = type + ' / ' + name;
  $('modal-body').innerHTML = '<div class="wt-loading"><div class="wt-spinner"></div><span class="wt-load-label">Report laden…</span></div>';
  modal.classList.add('show');
  try {
    const r = await fetch('/api/report/' + type + '/' + encodeURIComponent(name));
    $('modal-body').textContent = r.ok ? await r.text() : 'Error loading report';
  } catch(e) {
    $('modal-body').textContent = 'Error: ' + e.message;
  }
}

function closeModal() {
  $('report-modal').classList.remove('show');
}

async function openFile(type, name) {
  try {
    await fetch('/api/open/' + type + '/' + encodeURIComponent(name));
  } catch(e) {
    console.error('Open file error:', e);
  }
}

// Close modal on Escape or backdrop click
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
$('report-modal').addEventListener('click', e => { if (e.target === $('report-modal')) closeModal(); });

// Countdown-Balken über der Uhr: läuft in REFRESH ms linear von voll auf leer.
function runCountdown() {
  const bar = $('rf-bar');
  if (!bar) return;
  bar.style.transition = 'none';
  bar.style.transform = 'scaleX(1)';
  void bar.offsetWidth;                // Reflow, damit der Reset sofort greift
  bar.style.transition = 'transform ' + REFRESH + 'ms linear';
  bar.style.transform = 'scaleX(0)';
}

function update(d) {
  // Clock
  $('clock').textContent = new Date(d.ts).toLocaleTimeString('en');
  $('snap-count').textContent = d.snap_total.toLocaleString('en');

  // Pulse: kurze Impulsanimation am grünen Kreis bei jeder Aktualisierung
  const pulse = $('pulse');
  if (pulse) {
    pulse.classList.remove('pulsing');
    void pulse.offsetWidth;            // Reflow erzwingen, damit die Animation neu startet
    pulse.classList.add('pulsing');
  }
  // Countdown-Balken bis zur nächsten Aktualisierung neu starten
  runCountdown();

  // Live
  const lv = d.live;
  if (lv) {
    $('live-app').textContent = lv.app || '—';
    $('live-window').textContent = lv.window || '—';

    if (lv.media && lv.media.title) {
      let mt = (lv.media.service || lv.media.app || '') + ': ' + lv.media.title;
      if (lv.media.artist) mt += ' — ' + lv.media.artist;
      $('live-media').textContent = '♫ ' + mt;
      $('live-media').style.display = '';
    } else {
      $('live-media').style.display = 'none';
    }

    // Meters
    const kp = Math.min(lv.keys_pm / 200 * 100, 100);
    const cp = Math.min(lv.clicks_pm / 80 * 100, 100);
    const sp = Math.min(lv.scroll_pm / 200 * 100, 100);
    $('m-keys').style.width = kp + '%';
    $('m-clicks').style.width = cp + '%';
    $('m-scroll').style.width = sp + '%';
    $('v-keys').textContent = lv.keys_pm;
    $('v-clicks').textContent = lv.clicks_pm;
    $('v-scroll').textContent = lv.scroll_pm;

    // Idle
    const idle = Math.max(lv.idle_kb || 0, lv.idle_ms || 0);
    const ib = $('idle-banner');
    if (idle > 60) {
      ib.textContent = '⏸ Idle seit ' + Math.round(idle) + 's';
      ib.classList.add('show');
    } else {
      ib.classList.remove('show');
    }

    // System
    let sys = [];
    if (lv.system) {
      if (lv.system.battery_pct != null) {
        let bt = 'Akku: ' + lv.system.battery_pct + '%';
        if (lv.system.battery_charging) bt += ' ⚡';
        sys.push(bt);
      }
      if (lv.system.brightness != null)
        sys.push('Helligkeit: ' + Math.round(lv.system.brightness * 100) + '%');
      if (lv.system.active_space != null)
        sys.push('Space: ' + lv.system.active_space);
    }
    if (lv.git && lv.git.repo)
      sys.push('Git: ' + lv.git.repo + '/' + (lv.git.branch || '—'));
    $('sys-info').textContent = sys.join('  ·  ');
  }

  // Services — show uptime ("läuft · 27m 03s") when collector is running
  const svcs = d.services;
  let sh = '';
  const svcList = [
    ['collector', 'Collector', (d.interval || 10) + 's interval'],
    ['agg_daily', 'Agg Daily', 'daily 22:00'],
    ['agg_weekly', 'Agg Weekly', 'Sun 22:30'],
    ['agg_monthly', 'Agg Monthly', '1st of month 00:30'],
  ];
  for (const [key, name, sched] of svcList) {
    const s = svcs[key];
    let dot = 'off', info = 'not loaded';
    let state = 'off';   // off | running | loaded
    if (s) {
      const running = s.running || (s.loaded && s.pid);
      if (running) {
        state = 'running';
        dot = 'on';
        const uptime = s.uptime_sec;
        if (uptime && uptime > 0) {
          info = 'läuft · ' + fmtUptime(uptime);
        } else if (s.pid) {
          info = 'läuft · PID ' + s.pid;
        } else {
          info = 'läuft';
        }
      } else if (s.loaded) {
        state = 'loaded';
        dot = 'sched';
        let exitTag = '';
        if (s.exit != null) {
          const cls = s.exit === 0 ? 'exit-ok' : 'exit-err';
          exitTag = '  <span class="' + cls + '">exit=' + s.exit + '</span>';
        }
        info = sched + exitTag;
      }
    }
    // Only sensible actions for the current state:
    //   off      → Start
    //   running  → Restart, Stop
    //   loaded   → Start, Restart, Stop  (loaded but not running)
    let actions;
    if (state === 'off') actions = ['start'];
    else if (state === 'running') actions = ['restart', 'stop'];
    else actions = ['start', 'restart', 'stop'];
    const btns = actions.map(a =>
      '<button class="svc-btn svc-' + a + '" data-key="' + key + '" data-action="' + a + '"'
      + ' title="' + SVC_ACTION_LABEL[a] + ' ' + name + '">' + SVC_ACTION_LABEL[a] + '</button>'
    ).join('');
    sh += '<div class="svc"><span class="svc-dot ' + dot + '"></span>'
        + '<span class="svc-name">' + name + '</span>'
        + '<span class="svc-info">' + info + '</span>'
        + '<span class="svc-actions">' + btns + '</span></div>';
  }
  $('services').innerHTML = sh;

  // Day stats
  const dy = d.day;
  if (dy) {
    $('day-title').textContent = 'Today · ' + d.today;

    // Hero gauges (SVG rings)
    const workRatio = (dy.total_sec || 0) / (8 * 3600);
    const pctWork = Math.min(1, workRatio);
    const pctFocus = Math.min(1, (dy.focus_count || 0) / 4.0);
    const pctSessions = Math.min(1, (dy.sessions || 0) / 20.0);
    const topicsCount = (dy.topics && dy.topics.length) || 0;
    const pctInt = Math.min(1, (dy.avg_intensity || 0) / 10);
    const focusQ = dy.total_sec ? (dy.focus_sec || 0) / dy.total_sec : 0;
    const pauseQ = dy.span_sec ? (dy.pause_sec || 0) / dy.span_sec : 0;
    const swPh = dy.switches_ph || 0;
    const gauges = [
      {combo: true},
      {label: 'Sessions', value: dy.sessions, pct: pctSessions, color: 'var(--cyan)'},
      {label: 'Intensität', value: dy.avg_intensity != null ? dy.avg_intensity : '–', pct: pctInt,
       color: pctInt >= 0.6 ? 'var(--green)' : pctInt >= 0.3 ? 'var(--cyan)' : 'var(--yellow)',
       sub: 'von 10'},
      {label: 'Switches', value: swPh + '/h', pct: Math.min(1, swPh / 20),
       color: swPh <= 8 ? 'var(--green)' : swPh <= 14 ? 'var(--yellow)' : 'var(--red)',
       sub: dy.switches + ' gesamt'},
    ];
    $('gauges-row').innerHTML = gauges.map(g =>
      g.combo ? comboGaugeHtml(dy.total_sec || 0, dy.pause_sec || 0, dy.focus_sec || 0)
              : gaugeHtml(g)).join('');
    $('gauges-row').style.display = '';

    const HS = dy.hourly_series || {};
    $('day-stats').innerHTML = [
      ['Active', fmt(dy.total_sec), 'active'],
      ['Sessions', (dy.sessions_ph || 0) + '/h (' + dy.sessions + ' gesamt)', 'sessions'],
      ['Focus', dy.focus_count + ' (' + fmt(dy.focus_sec) + ')', 'focus'],
      ['Switches', (dy.switches_ph || 0) + '/h (' + dy.switches + ' gesamt)', 'switches'],
      ['Keys', (dy.keys_ph || 0).toLocaleString('en') + '/h (' + dy.keys.toLocaleString('en') + ' gesamt)', 'keys'],
      ['Clicks', (dy.clicks_ph || 0).toLocaleString('en') + '/h (' + dy.clicks.toLocaleString('en') + ' gesamt)', 'clicks'],
      ['Scroll', (dy.scrolls_ph || 0).toLocaleString('en') + '/h (' + dy.scrolls.toLocaleString('en') + ' gesamt)', 'scrolls'],
      ['Scroll-Distanz', scrollDist(dy.scroll_m || 0), 'scrolls'],
      ['Clipboard', (dy.clipboard_ph || 0) + '/h (' + dy.clipboard + 'x gesamt)', 'clipboard'],
      ['Topics', topicsCount, 'topics'],
      ['Start', (dy.first_start || '–') + ' (bis ' + (dy.last_end || '–') + ')', 'active'],
      ['Pause', fmt(dy.pause_sec || 0), null],
      ['Ø Session', fmt(dy.avg_session_sec || 0), 'avg_session'],
      ['Längste', fmt(dy.longest_session_sec || 0), 'longest'],
      ['Intensität', dy.avg_intensity != null ? dy.avg_intensity : '–', 'intensity'],
      ['Peak', dy.peak_hour != null ? dy.peak_hour + '–' + ((dy.peak_hour + 1) % 24) + 'h' : '–', 'active', dy.peak_hour],
      ['Apps', dy.app_count || 0, 'apps'],
      ['Projekte', dy.project_count || 0, 'projects'],
      ['Screenshots', (dy.screenshots_ph || 0) + '/h (' + (dy.screenshots || 0) + ' gesamt)', 'screenshots'],
    ].map(([l, v, sk, hi]) => {
      const val = String(v).replace(/\s*(\([^)]*\))\s*$/, '<span class="stat-sub">$1</span>');
      const sp = sk ? spark(HS[sk], hi) : '<div class="stat-spark-empty"></div>';
      return '<div class="stat"><div class="stat-val">' + val + '</div>' + sp + '<div class="stat-label">' + l + '</div></div>';
    }).join('');

    // App Categories with sub-apps
    let catHtml = '';
    (dy.app_categories || []).forEach((c, i) => {
      const col = PROJ_COLORS[i % PROJ_COLORS.length];
      const catId = 'cat-' + i;
      const hasApps = c.apps && c.apps.length > 0;
      const open = expandedCats.has(c.name);
      catHtml += '<div class="proj-row' + (hasApps ? ' cat-toggle' : '') + '"'
          + (hasApps ? ' onclick="toggleCat(\''+esc(c.name)+'\',\''+catId+'\')"' : '') + '>'
          + (hasApps ? '<span class="cat-arrow'+(open?' open':'')+'">▸</span>' : '<span class="cat-arrow-spacer"></span>')
          + '<div class="proj-name">' + esc(c.name) + '</div>'
          + '<div class="proj-bar-wrap"><div class="proj-bar" style="width:' + c.pct + '%;background:' + col + '"></div></div>'
          + '<div class="proj-pct">' + c.pct + '%</div>'
          + '<div class="proj-time">' + fmt(c.sec) + '</div>'
          + '</div>';
      if (hasApps) {
        catHtml += '<div class="cat-subs" id="'+catId+'" style="display:'+(open?'block':'none')+'">';
        c.apps.forEach(a => {
          catHtml += '<div class="proj-row sub-row">'
              + '<span class="cat-arrow-spacer"></span>'
              + '<div class="proj-name sub-name">' + esc(a.name) + '</div>'
              + '<div class="proj-bar-wrap"><div class="proj-bar" style="width:' + a.pct + '%;background:' + col + ';opacity:0.5"></div></div>'
              + '<div class="proj-pct">' + a.pct + '%</div>'
              + '<div class="proj-time">' + fmt(a.sec) + '</div>'
              + '</div>';
        });
        catHtml += '</div>';
      }
    });
    $('app-categories').innerHTML = catHtml;

    // Web Categories (3-level tree)
    let wcHtml = '';
    (dy.web_categories || []).forEach((wc, i) => {
      const col = PROJ_COLORS[i % PROJ_COLORS.length];
      const wcId = 'wcat-' + i;
      const hasSubs = wc.subcategories && wc.subcategories.length > 0;
      const openMain = expandedWebCats.has(wc.name);
      wcHtml += '<div class="proj-row' + (hasSubs ? ' cat-toggle' : '') + '"'
          + (hasSubs ? ' onclick="toggleWebCat(\''+esc(wc.name)+'\',\''+wcId+'\')"' : '') + '>'
          + (hasSubs ? '<span class="cat-arrow'+(openMain?' open':'')+'">▸</span>' : '<span class="cat-arrow-spacer"></span>')
          + '<div class="proj-name">' + esc(wc.name) + '</div>'
          + '<div class="proj-bar-wrap"><div class="proj-bar" style="width:' + wc.pct + '%;background:' + col + '"></div></div>'
          + '<div class="proj-pct">' + wc.pct + '%</div>'
          + '<div class="proj-time">' + fmt(wc.sec) + '</div>'
          + '</div>';
      if (hasSubs) {
        wcHtml += '<div class="cat-subs" id="'+wcId+'" style="display:'+(openMain?'block':'none')+'">';
        wc.subcategories.forEach((sc, j) => {
          const scId = wcId + '-s-' + j;
          const hasDoms = sc.domains && sc.domains.length > 0;
          const scKey = wc.name + '/' + sc.name;
          const openSub = expandedWebCats.has(scKey);
          wcHtml += '<div class="proj-row sub-row' + (hasDoms ? ' cat-toggle' : '') + '"'
              + (hasDoms ? ' onclick="event.stopPropagation();toggleWebCat(\''+esc(scKey)+'\',\''+scId+'\')"' : '') + '>'
              + (hasDoms ? '<span class="cat-arrow'+(openSub?' open':'')+'">▸</span>' : '<span class="cat-arrow-spacer"></span>')
              + '<div class="proj-name sub-name">' + esc(sc.name) + '</div>'
              + '<div class="proj-bar-wrap"><div class="proj-bar" style="width:' + sc.pct + '%;background:' + col + ';opacity:0.5"></div></div>'
              + '<div class="proj-pct">' + sc.pct + '%</div>'
              + '<div class="proj-time">' + fmt(sc.sec) + '</div>'
              + '</div>';
          if (hasDoms) {
            wcHtml += '<div class="wcat-subs" id="'+scId+'" style="display:'+(openSub?'block':'none')+'">';
            sc.domains.forEach(d => {
              wcHtml += '<div class="proj-row wcat-domain">'
                  + '<span class="cat-arrow-spacer"></span>'
                  + '<div class="proj-name">' + esc(d.domain) + '</div>'
                  + '<div class="proj-bar-wrap"><div class="proj-bar" style="width:' + d.pct + '%;background:' + col + ';opacity:0.3"></div></div>'
                  + '<div class="proj-pct">' + d.pct + '%</div>'
                  + '<div class="proj-time">' + fmt(d.sec) + '</div>'
                  + '</div>';
            });
            wcHtml += '</div>';
          }
        });
        wcHtml += '</div>';
      }
    });
    $('web-categories').innerHTML = wcHtml || '<div style="color:var(--fg2)">Keine Browser-Daten</div>';

    // Projects
    $('projects').innerHTML = paginated('dash-proj', dy.projects || [], (p, i) => {
      const c = PROJ_COLORS[i % PROJ_COLORS.length];
      return '<div class="proj-row">'
          + '<div class="proj-name">' + esc(p.name) + '</div>'
          + '<div class="proj-bar-wrap"><div class="proj-bar" style="width:' + p.pct + '%;background:' + c + '"></div></div>'
          + '<div class="proj-pct">' + p.pct + '%</div>'
          + '<div class="proj-time">' + fmt(p.sec) + '</div>'
          + '<div class="proj-int">' + p.intensity + '</div>'
          + '</div>';
    }, 10);

    // Topics (aggregated from per-session LLM extraction)
    const topics = dy.topics || [];
    if (topics.length) {
      const topicTotal = topics.reduce((a, t) => a + (t.sec || 0), 0);
      const topicMax = Math.max(...topics.map(t => t.sec || 0), 1);
      $('topics-meta').textContent = topics.length + ' · ' + fmt(topicTotal);
      $('topics').innerHTML = paginated('dash-topics', topics, (t) => {
        const widthPct = Math.round(((t.sec || 0) / topicMax) * 100);
        const proj = t.project ? '<span class="topic-proj">' + esc(t.project) + '</span>' : '';
        const long = (t.topic_long || '').trim();
        const rowAttrs = long
            ? 'class="topic-row has-long" title="' + esc(long).replace(/"/g, '&quot;') + '"'
            : 'class="topic-row"';
        return '<div ' + rowAttrs + '>'
            + '<div class="topic-name">' + esc(t.name) + '</div>'
            + proj
            + '<div class="topic-bar-wrap"><div class="topic-bar" style="width:' + widthPct + '%"></div></div>'
            + '<div class="topic-time">' + fmt(t.sec) + '</div>'
            + '<div class="topic-sessions">×' + (t.sessions || 1) + '</div>'
            + '</div>';
      }, 10);
    } else {
      $('topics-meta').textContent = '';
      $('topics').innerHTML = '<div style="color:var(--fg3);font-size:12px;padding:8px 0">Noch keine Themen erkannt. Topics werden vom lokalen LLM beim Aggregieren erzeugt.</div>';
    }

    // Apps
    $('apps').innerHTML = paginated('dash-apps', dy.apps || [], (a, i) => {
      const c = PROJ_COLORS[i % PROJ_COLORS.length];
      return '<div class="proj-row">'
          + '<div class="proj-name">' + esc(a.name) + '</div>'
          + '<div class="proj-bar-wrap"><div class="proj-bar" style="width:' + a.pct + '%;background:' + c + '"></div></div>'
          + '<div class="proj-pct">' + a.pct + '%</div>'
          + '<div class="proj-time">' + fmt(a.sec) + '</div>'
          + '</div>';
    });

    // Hourly chart — starts at 06:00 and wraps through midnight to 05:00.
    // Display order: [6, 7, ..., 23, 0, 1, ..., 5]
    const maxH = Math.max(...dy.hourly, 1);
    const nowH = new Date().getHours();
    const DAY_START_HOUR = 6;
    let ch = '';
    for (let i = 0; i < 24; i++) {
      const h = (DAY_START_HOUR + i) % 24;       // actual wall-clock hour
      const pct = dy.hourly[h] / maxH * 100;
      const cls = h === nowH ? ' now' : '';
      // Label every 3rd column to keep the axis readable
      const lbl = (i % 3 === 0 || i === 23) ? String(h).padStart(2, '0') : '';
      ch += '<div class="chart-bar-wrap">'
          + '<div class="chart-bar' + cls + '" style="height:' + pct + '%"></div>'
          + '<div class="chart-lbl">' + lbl + '</div>'
          + '</div>';
    }
    $('hourly-chart').innerHTML = ch;
  } else {
    $('gauges-row').style.display = 'none';
    $('day-stats').innerHTML = '<div style="color:var(--fg2)">Noch keine Daten</div>';
  }

  // Recent sessions
  const sessItems = d.recent_sessions || [];
  const maxDur = Math.max(...sessItems.map(s => s.dur || 0), 1);
  const sessHtml = paginated('dash-sess', sessItems, (s) => {
    const durPct = Math.round((s.dur || 0) / maxDur * 100);
    const iv = Math.round(Math.min(s.intensity || 0, 10));
    let bars = '';
    for (let b = 0; b < 5; b++) {
      const lvl = b * 2;
      const on = iv > lvl;
      const col = iv <= 3 ? 'var(--cyan)' : iv <= 6 ? 'var(--yellow)' : 'var(--red)';
      const bg = on ? col : 'var(--bg3)';
      const h = 3 + b * 2.5;
      bars += '<div class="sess-int-seg" style="height:'+h+'px;background:'+bg+'"></div>';
    }
    const topicHtml = s.topic ? esc(s.topic) : '';
    const scrN = s.screenshots || 0;
    const scrHtml = scrN && s.screenshot_url
      ? '<a class="sess-scr" href="'+esc(s.screenshot_url)+'" target="_blank" title="'+scrN+' Screenshot'+(scrN>1?'s':'')+'">📷 '+scrN+'</a>'
      : '<span class="sess-scr-empty"></span>';
    return '<div class="sess-row">'
        + '<div class="sess-time">' + s.time + '</div>'
        + '<div class="sess-app">' + esc(s.app) + '</div>'
        + '<div class="sess-title">' + esc(s.title) + '</div>'
        + '<div class="sess-topic">' + topicHtml + '</div>'
        + '<div class="sess-proj">' + esc(s.project) + '</div>'
        + '<div class="sess-dur-wrap"><div class="sess-dur">' + fmt(s.dur) + '</div>'
        + '<div class="sess-dur-bar"><div class="sess-dur-fill" style="width:' + durPct + '%"></div></div></div>'
        + '<div class="sess-int-wrap">' + bars + '</div>'
        + scrHtml
        + '</div>';
  });
  $('sessions').innerHTML = sessHtml || '<div style="color:var(--fg2)">—</div>';

  // Reports
  let rh = '';
  for (const [type, label] of [['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly']]) {
    const g = d.report_groups ? d.report_groups[type] : null;
    rh += '<div class="rpt-group">';
    rh += '<div class="rpt-group-label">' + label + '</div>';
    if (g) {
      for (const key of ['short', 'summary', 'raw']) {
        const f = g[key];
        if (f) {
          const kb = (f.size / 1024).toFixed(1);
          rh += '<div class="rpt">'
              + '<span class="rpt-view" onclick="openReport(\'' + type + '\',\'' + esc(f.name) + '\')" title="Preview">👁</span>'
              + '<span class="rpt-name" onclick="openFile(\'' + type + '\',\'' + esc(f.name) + '\')" title="Open in editor">' + esc(f.name) + '</span>'
              + '<span class="rpt-size">' + kb + ' KB</span>'
              + '<span class="rpt-age">' + fmtAge(f.mtime) + '</span>'
              + '</div>';
        }
      }
    } else {
      rh += '<div style="color:var(--fg3);font-size:12px;padding:2px 0">—</div>';
    }
    rh += '</div>';
  }
  $('reports').innerHTML = rh;

  // Logs — color-code timestamp + level
  $('logs').innerHTML = (d.logs || []).map(l => {
    const m = l.match(/^(\S+\s+\S+)\s+\[(INFO|WARN|WARNING|ERROR|ERR)\]\s*(.*)$/);
    if (m) {
      const lvl = m[2].toUpperCase();
      const cls = lvl.startsWith('ERR') ? 'error' : lvl.startsWith('WARN') ? 'warn' : 'info';
      return '<div class="log-line"><span class="log-ts">' + esc(m[1]) + '</span>'
           + '<span class="log-lvl ' + cls + '">' + esc(lvl) + '</span>'
           + esc(m[3]) + '</div>';
    }
    return '<div class="log-line">' + esc(l) + '</div>';
  }).join('');
}

function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

const _expandedLists = new Set(JSON.parse(localStorage.getItem('wt_expanded_lists') || '[]'));
function _toggleList(id, showN) {
  if (_expandedLists.has(id)) _expandedLists.delete(id); else _expandedLists.add(id);
  localStorage.setItem('wt_expanded_lists', JSON.stringify([..._expandedLists]));
  var m = document.getElementById(id+'-more');
  var pg = document.getElementById(id+'-pages');
  var btn = document.getElementById(id+'-btn');
  var open = _expandedLists.has(id);
  if (m) m.style.display = open ? 'block' : 'none';
  if (pg) pg.style.display = open ? 'block' : 'none';
  if (btn) btn.textContent = open ? 'Weniger anzeigen' : 'Alle '+showN+' anzeigen';
}

function paginated(id, items, rowFn, initCount) {
  const INIT = initCount || 15, PAGE = 50;
  const open = _expandedLists.has(id);
  let h = '';
  items.slice(0, INIT).forEach((it, i) => { h += rowFn(it, i); });
  if (items.length > INIT) {
    h += '<div id="'+id+'-more" style="display:'+(open?'block':'none')+'">';
    items.slice(INIT, PAGE).forEach((it, i) => { h += rowFn(it, INIT + i); });
    h += '</div>';
    const showN = Math.min(items.length, PAGE);
    h += '<a href="#" class="dist-toggle" id="'+id+'-btn" onclick="'
      + 'event.preventDefault();_toggleList(\''+id+'\','+showN+');'
      + '">'+(open?'Weniger anzeigen':'Alle '+showN+' anzeigen')+'</a>';
  }
  if (items.length > PAGE) {
    h += '<div id="'+id+'-pages" style="display:'+(open?'block':'none')+'">';
    const pages = Math.ceil((items.length - PAGE) / PAGE);
    for (let p = 0; p < pages; p++) {
      const start = PAGE + p * PAGE;
      const end = Math.min(start + PAGE, items.length);
      h += '<div id="'+id+'-page-'+p+'" style="display:'+(p===0?'block':'none')+'">';
      items.slice(start, end).forEach((it, i) => { h += rowFn(it, start + i); });
      h += '</div>';
    }
    h += '<div class="dist-pagination">';
    for (let p = 0; p < pages; p++) {
      const start = PAGE + p * PAGE;
      const end = Math.min(start + PAGE, items.length);
      if (start >= items.length) break;
      h += '<a href="#" class="dist-page-btn'+(p===0?' active':'')+'" onclick="'
        + 'event.preventDefault();'
        + 'document.querySelectorAll(\'#'+id+'-pages>div[id]\').forEach(function(el){el.style.display=\'none\'});'
        + 'document.getElementById(\''+id+'-page-'+p+'\').style.display=\'block\';'
        + 'this.parentNode.querySelectorAll(\'.dist-page-btn\').forEach(function(b){b.classList.remove(\'active\')});'
        + 'this.classList.add(\'active\');'
        + '">'+(start+1)+'–'+end+'</a>';
    }
    h += '</div></div>';
  }
  return h;
}

async function refresh() {
  try {
    const r = await fetch('/api/live');
    if (r.ok) update(await r.json());
  } catch (e) {
    console.error('Refresh error:', e);
  }
}

const SVC_ACTION_LABEL = { start: 'Start', stop: 'Stop', restart: 'Restart' };
let _svcBusy = false;

async function svcAction(key, action, btn) {
  if (_svcBusy) return;
  _svcBusy = true;
  const row = btn.closest('.svc');
  if (row) row.classList.add('svc-working');
  btn.disabled = true;
  try {
    const r = await fetch('/api/service/' + key + '/' + action, { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || r.statusText);
    // launchd needs a moment; give it one before re-reading status.
    await new Promise(res => setTimeout(res, 700));
    await refresh();
  } catch (e) {
    console.error('Service action failed:', e);
    if (row) row.classList.remove('svc-working');
    btn.disabled = false;
  } finally {
    _svcBusy = false;
  }
}

// Delegation on the (static) #services container survives innerHTML rerenders.
document.getElementById('services').addEventListener('click', (e) => {
  const b = e.target.closest('.svc-btn');
  if (b && b.dataset.key) svcAction(b.dataset.key, b.dataset.action, b);
});

// Rhythm Heatmap
async function loadRhythm() {
  try {
    const r = await fetch('/api/rhythm/2');
    if (!r.ok) return;
    const data = await r.json();
    renderHeatmap(data);
  } catch(e) { console.error('Rhythm error:', e); }
}

function renderHeatmap(data) {
  const el = $('rhythm-heatmap');
  if (!el) return;
  let html = '';

  // Hour labels — use wall-clock hours from the API (06-06 day).
  const displayHours = data.display_hours || Array.from({length: 24}, (_, i) => i);
  html += '<div class="heatmap-hours">';
  for (let i = 0; i < displayHours.length; i++) {
    const hr = displayHours[i];
    const showLabel = (i % 3 === 0) || (i === displayHours.length - 1);
    html += '<span>' + (showLabel ? String(hr).padStart(2, '0') : '') + '</span>';
  }
  html += '</div>';

  let prevSun = false;
  data.days.forEach((d, i) => {
    // Week separator
    if (new Date(d.date).getDay() === 1 && i > 0) {
      html += '<hr class="heatmap-sep">';
    }

    const cls = d.today ? 'today' : d.weekend ? 'weekend' : '';
    const dd = d.date.slice(5); // MM-DD
    html += '<div class="heatmap-row">';
    html += '<div class="heatmap-label ' + cls + '">' + d.weekday + ' ' + dd.replace('-', '.') + '</div>';
    d.hours.forEach((c, idx) => {
      const hr = displayHours[idx];
      const nh = (hr + 1) % 24;
      const range = String(hr).padStart(2,'0') + ':00 - ' + String(nh).padStart(2,'0') + ':00';
      const kind = c === 'healthy' ? 'Tag' : c === 'unhealthy' ? 'Nacht' : 'inaktiv';
      html += '<div class="heatmap-cell ' + c + '" title="' + d.date + ' ' + range + ' — ' + kind + '"></div>';
    });
    html += '<div class="heatmap-day-total">' + (d.active > 0 ? d.active + 'h' : '') + '</div>';
    html += '</div>';
  });

  // Legend
  html += '<div class="heatmap-legend">';
  html += '<span><span class="heatmap-legend-dot" style="background:#6cb6ff"></span>Tag (06–20)</span>';
  html += '<span><span class="heatmap-legend-dot" style="background:#1f3a8a"></span>Nacht (20–06)</span>';
  html += '<span><span class="heatmap-legend-dot" style="background:var(--bg3)"></span>Inaktiv</span>';
  html += '</div>';

  // Stats
  const s = data.stats;
  html += '<div class="heatmap-stats">';
  html += '<div class="stat"><div class="stat-val">' + s.avg_active + 'h</div><div class="stat-label">Avg/Day</div></div>';
  html += '<div class="stat"><div class="stat-val" style="color:#6cb6ff">' + s.day_hours + 'h</div><div class="stat-label">Tagesarbeitszeit</div></div>';
  html += '<div class="stat"><div class="stat-val" style="color:#1f6feb">' + s.night_hours + 'h</div><div class="stat-label">Nachtarbeitszeit</div></div>';

  // Tag/Nacht ratio donut
  const tot = (s.day_hours || 0) + (s.night_hours || 0);
  const dayPct = tot ? Math.round(s.day_hours / tot * 100) : 0;
  const nightPct = tot ? 100 - dayPct : 0;
  const R = 18, C = 2 * Math.PI * R;
  const dayLen = tot ? (s.day_hours / tot) * C : 0;
  html += '<div class="heatmap-donut">'
        + '<svg width="48" height="48" viewBox="0 0 48 48">'
        + '<circle cx="24" cy="24" r="' + R + '" fill="none" stroke="#1f3a8a" stroke-width="7"/>'
        + '<circle cx="24" cy="24" r="' + R + '" fill="none" stroke="#6cb6ff" stroke-width="7"'
        + ' stroke-dasharray="' + dayLen.toFixed(2) + ' ' + (C - dayLen).toFixed(2) + '"'
        + ' transform="rotate(-90 24 24)" stroke-linecap="round"/>'
        + '<text x="24" y="25" text-anchor="middle" dominant-baseline="middle" '
        + 'font-size="12" font-weight="700" fill="#6cb6ff">' + dayPct + '%</text>'
        + '</svg>'
        + '<div class="heatmap-donut-legend">'
        + '<div><span class="dn-dot" style="background:#6cb6ff"></span>'
        + '<span class="dn-pct">' + dayPct + '%</span> <span class="dn-name">Tag</span></div>'
        + '<div><span class="dn-dot" style="background:#1f3a8a"></span>'
        + '<span class="dn-pct">' + nightPct + '%</span> <span class="dn-name">Nacht</span></div>'
        + '</div>'
        + '</div>';

  html += '<div class="stat"><div class="stat-val">' + s.days_tracked + '/' + s.total_days + '</div><div class="stat-label">Days tracked</div></div>';
  html += '</div>';

  el.innerHTML = html;
}

refresh();
loadRhythm();
setInterval(refresh, REFRESH);
setInterval(loadRhythm, 60000);
</script>
</body>
</html>"""


EXPLORE_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkTracker — Explore</title>
<style>
:root {
  /* Palette mirrors docs/index.html — acid-green accent, slate-blue cyan */
  --bg: #0c1116; --bg2: #141a20; --bg3: #1b222a;
  --fg: #cfd7e0; --fg2: #8a95a3; --fg3: #484f58;
  --cyan: #4fc3d8; --green: #6fe28a; --yellow: #d29922; --acid: #d4f500;
  --red: #ff4d4f; --purple: #b392ff; --blue: #4fc3d8;
  --border: #2b3642; --white: #f4f8ff;
  --orange: #d18616;
  --card-top: #161e26; --card-hover: #3a4754;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  background: var(--bg); color: var(--fg);
  font-size: 13px; line-height: 1.5;
  padding: 16px clamp(16px, 2.5vw, 36px) 48px; max-width: 1760px; margin: 0 auto;
}
a { color: var(--cyan); text-decoration: none; }
a:hover { text-decoration: underline; }
h2 {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.8px;
  color: var(--fg2); margin-bottom: 8px; padding-bottom: 5px;
  border-bottom: 1px solid var(--border);
}
h2::before {
  content: ''; display: inline-block; width: 7px; height: 7px;
  background: var(--acid); border-radius: 2px; margin-right: 8px;
}
.card {
  background: linear-gradient(180deg, var(--card-top) 0%, var(--bg2) 55%);
  border: 1px solid var(--border);
  border-radius: 14px; padding: 16px; margin-bottom: 14px;
  box-shadow: 0 1px 2px rgba(0,0,0,.25), 0 14px 30px -24px rgba(0,0,0,.6);
  transition: border-color 0.15s, box-shadow 0.2s;
}
.card:hover { border-color: var(--card-hover);
  box-shadow: 0 2px 4px rgba(0,0,0,.25), 0 18px 36px -22px rgba(0,0,0,.65); }

/* Header */
.header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 0 12px; border-bottom: 1px solid var(--bg3); margin-bottom: 16px;
}
.header h1 { font-size: 18px; color: var(--cyan); font-weight: 600; }
.header-right { color: var(--fg2); font-size: 12px; }

/* Date Nav */
.date-nav {
  display: flex; align-items: center; gap: 12px; margin-bottom: 16px;
  flex-wrap: wrap;
}
.date-nav button {
  background: var(--bg3); border: 1px solid var(--fg3); color: var(--fg);
  border-radius: 6px; padding: 6px 12px; cursor: pointer;
  font-family: inherit; font-size: 12px;
}
.date-nav button:hover { background: var(--bg2); border-color: var(--cyan); }
.date-nav button:disabled { opacity: 0.3; cursor: not-allowed; }
/* Datepicker — identisch zur Screenshots-Seite (nur Tage mit Daten klickbar) */
.dp { position: relative; }
.dp-trigger {
  display: flex; align-items: center; gap: 8px;
  background: var(--bg2); color: var(--fg); border: 1px solid var(--bg3);
  border-radius: 6px; padding: 6px 12px; font-family: inherit;
  font-size: 14px; font-weight: 700; cursor: pointer; transition: border-color 0.15s;
}
.dp-trigger:hover { border-color: var(--cyan); }
.dp-trigger svg { color: var(--cyan); }
.dp-pop {
  position: absolute; top: calc(100% + 6px); left: 0; z-index: 60;
  background: var(--bg2); border: 1px solid var(--bg3); border-radius: 8px;
  padding: 10px; width: 232px; box-shadow: 0 8px 24px rgba(0,0,0,0.5);
}
.dp-pop[hidden] { display: none; }
.dp-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.dp-title { font-size: 12px; font-weight: 600; color: var(--fg); }
.dp-nav {
  background: none; border: none; color: var(--fg2); cursor: pointer;
  font-size: 18px; line-height: 1; padding: 2px 8px; border-radius: 5px;
}
.dp-nav:hover:not(:disabled) { background: var(--bg3); color: var(--fg); }
.dp-nav:disabled { opacity: 0.25; cursor: default; }
.dp-weekdays, .dp-grid {
  display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px;
}
.dp-weekdays span {
  text-align: center; font-size: 9px; color: var(--fg3);
  text-transform: uppercase; padding: 2px 0;
}
.dp-day {
  text-align: center; font-size: 11px; padding: 5px 0; border-radius: 5px;
  color: var(--fg3); user-select: none;
}
.dp-day.has {
  color: var(--fg); cursor: pointer; background: var(--bg3);
  position: relative;
}
.dp-day.has:hover { background: var(--cyan); color: var(--bg); }
.dp-day.sel { background: var(--cyan); color: var(--bg); font-weight: 700; }
.dp-day.dp-empty { visibility: hidden; padding: 0; }
.dp-day.today:not(.sel) { box-shadow: inset 0 0 0 1px var(--cyan); }
.dp-day.muted { color: var(--bg3); }

/* Stats */
.stats { display: flex; flex-wrap: wrap; gap: 6px 20px; }
.stat { text-align: center; min-width: 80px; }
.stat-val { font-size: 22px; font-weight: 700; color: var(--fg); }
.stat-label { font-size: 10px; color: var(--fg2); text-transform: uppercase; }

/* Distribution grid — auto-fit: nutzt auf breiten Screens 3 Spalten */
.dist-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(400px, 100%), 1fr)); gap: 12px; margin-top: 12px; }
.dist-card { background: var(--bg); border: 1px solid var(--bg3); border-radius: 6px; padding: 12px; }
.dist-search { width: 100%; padding: 5px 8px; margin-bottom: 8px; background: var(--bg2); border: 1px solid var(--bg3); border-radius: 4px; color: var(--fg); font-size: 12px; outline: none; box-sizing: border-box; }
.dist-search:focus { border-color: var(--cyan); }
.dist-search::placeholder { color: var(--fg3); }
.dist-toggle { display: block; text-align: center; padding: 6px 0; margin-top: 4px; color: var(--cyan); font-size: 12px; text-decoration: none; cursor: pointer; }
.dist-toggle:hover { text-decoration: underline; }
.dist-pagination { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--bg3); }
.dist-page-btn { font-size: 11px; padding: 3px 8px; border-radius: 4px; background: var(--bg3); color: var(--fg2); text-decoration: none; cursor: pointer; }
.dist-page-btn:hover { background: var(--cyan); color: var(--bg); }
.dist-page-btn.active { background: var(--cyan); color: var(--bg); }

/* Bar rows */
.bar-row {
  display: flex; align-items: center; gap: 8px; padding: 3px 0;
  border-bottom: 1px solid var(--bg3); font-size: 12px;
}
.bar-row:last-child { border-bottom: none; }
.bar-name {
  width: 120px; flex-shrink: 0; color: var(--fg); font-weight: 500;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.bar-wrap { flex: 1; height: 8px; background: var(--bg3); border-radius: 4px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
.bar-pct { width: 40px; text-align: right; color: var(--fg2); }
.bar-time { width: 55px; text-align: right; color: var(--fg2); }
.topic-dist-proj {
  font-size: 10px; color: var(--cyan);
  padding: 2px 7px; border: 1px solid var(--bg3); border-radius: 10px;
  max-width: 110px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.topic-dist-count {
  font-size: 10px; color: var(--fg3); min-width: 22px; text-align: right;
}

/* Timeline */
.timeline-wrap { position: relative; margin: 8px 0; }
.timeline-hours {
  display: flex; justify-content: space-between; font-size: 9px; color: var(--fg3);
  padding: 0 0 4px;
}
.timeline-svg { width: 100%; display: block; }
.timeline-tooltip {
  position: fixed; background: var(--bg2); border: 1px solid var(--fg3);
  border-radius: 6px; padding: 8px 12px; font-size: 11px; color: var(--fg);
  pointer-events: none; z-index: 50; display: none; max-width: 300px;
}

/* Filter bar */
.filter-bar {
  display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; align-items: center;
}
.filter-bar input, .filter-bar select {
  background: var(--bg); border: 1px solid var(--bg3); color: var(--fg);
  border-radius: 6px; padding: 5px 10px; font-family: inherit; font-size: 12px;
}
.filter-bar input:focus, .filter-bar select:focus { outline: none; border-color: var(--cyan); }
.filter-bar select { min-width: 100px; }
.filter-bar .sort-btn {
  background: none; border: 1px solid var(--bg3); color: var(--fg2);
  border-radius: 6px; padding: 5px 10px; cursor: pointer; font-family: inherit; font-size: 12px;
}
.filter-bar .sort-btn.active { color: var(--cyan); border-color: var(--cyan); }
.filter-count { color: var(--fg3); font-size: 11px; margin-left: auto; }

/* Session rows */
.sess-row {
  display: flex; gap: 8px; padding: 7px 8px; font-size: 12px; align-items: center;
  border-bottom: 1px solid var(--bg3); cursor: pointer; border-radius: 4px;
  transition: background 0.15s;
}
.sess-row:hover { background: var(--bg3); }
.sess-row.expanded { background: var(--bg3); border-bottom: none; border-radius: 4px 4px 0 0; }
.sess-time { width: 45px; color: var(--fg2); flex-shrink: 0; }
.sess-app {
  width: 120px; color: var(--fg); font-weight: 500; flex-shrink: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sess-title {
  flex: 1; color: var(--fg2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sess-proj {
  width: 100px; flex-shrink: 0; text-align: right; color: var(--blue); font-size: 11px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sess-topic {
  width: 150px; flex-shrink: 0; color: var(--cyan); font-size: 11px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-style: italic;
}
.sess-topic:empty { display: none; }
/* Topics mit topic_long: gepunktete Unterstreichung als Tooltip-Hinweis */
.sess-topic.has-long { cursor: help;
  text-decoration: underline dotted var(--fg3); text-underline-offset: 3px; }
.sess-tool {
  display: inline-block; margin-left: 6px; color: var(--cyan); font-size: 10px;
  opacity: 0.7; cursor: help;
}
.sess-dur-wrap { flex: 0.6; display: flex; align-items: center; gap: 6px; min-width: 100px; }
.sess-dur { flex-shrink: 0; text-align: right; color: var(--yellow); white-space: nowrap; }
.sess-dur-bar { flex: 1; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.sess-dur-fill { height: 100%; border-radius: 3px; background: #888; }
.sess-int-wrap { width: 36px; flex-shrink: 0; display: flex; align-items: flex-end; gap: 2px; height: 14px; }
.sess-int-seg { width: 4px; border-radius: 1px; }
.sess-scr { width: 44px; flex-shrink: 0; text-align: right; color: var(--fg2); font-size: 11px;
  text-decoration: none; white-space: nowrap; }
.sess-scr:hover { color: var(--cyan); }
.sess-scr-empty { width: 44px; flex-shrink: 0; }
.sess-snaps { width: 30px; flex-shrink: 0; text-align: right; color: var(--fg3); font-size: 11px; }
.sess-arrow { width: 16px; flex-shrink: 0; color: var(--fg3); text-align: center; transition: transform 0.2s; font-size: 10px; }
.sess-row.expanded .sess-arrow { transform: rotate(90deg); }

/* Session detail */
.sess-detail {
  background: var(--bg3); border: 1px solid var(--fg3); border-top: none;
  border-radius: 0 0 6px 6px; padding: 14px; margin-bottom: 6px;
  display: none;
}
.sess-detail.show { display: block; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
@media (max-width: 600px) { .detail-grid { grid-template-columns: 1fr; } }
.detail-field { font-size: 12px; }
.detail-label { color: var(--fg3); font-size: 10px; text-transform: uppercase; margin-bottom: 2px; }
.detail-val { color: var(--fg); word-break: break-all; }
/* topic_long ist Fließtext — normale Umbrüche statt break-all */
.detail-val.topic-long-val { word-break: normal; overflow-wrap: break-word;
  color: var(--fg2); font-style: italic; line-height: 1.55; }

/* Input mini bars */
.input-bars { display: flex; gap: 16px; margin-top: 8px; }
.input-bar { flex: 1; }
.input-bar-label { font-size: 10px; color: var(--fg2); }
.input-bar-track { height: 6px; background: var(--bg); border-radius: 3px; overflow: hidden; margin-top: 2px; }
.input-bar-fill { height: 100%; border-radius: 3px; }
.input-bar-val { font-size: 11px; color: var(--fg); margin-top: 2px; }

/* Snapshot button */
.snap-btn {
  background: var(--bg); border: 1px solid var(--cyan); color: var(--cyan);
  border-radius: 6px; padding: 6px 14px; cursor: pointer;
  font-family: inherit; font-size: 12px; margin-top: 12px;
}
.snap-btn:hover { background: var(--bg2); }

/* Snapshot panel */
.snap-panel { margin-top: 12px; }
.snap-scrubber {
  display: flex; gap: 2px; flex-wrap: wrap; margin-bottom: 10px;
  padding: 6px; background: var(--bg); border-radius: 6px;
}
.snap-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--fg3); cursor: pointer; transition: all 0.15s;
}
.snap-dot:hover { background: var(--cyan); transform: scale(1.4); }
.snap-dot.active { background: var(--cyan); transform: scale(1.5); }
.snap-dot.has-input { background: var(--green); }
.snap-dot.has-input.active { background: var(--cyan); }

/* Snapshot detail */
.snap-detail-card {
  background: var(--bg); border: 1px solid var(--bg3); border-radius: 6px; padding: 14px;
}
.snap-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px; font-size: 12px;
}
.snap-ts { color: var(--cyan); font-weight: 600; }
.snap-app-info { color: var(--fg); }
.snap-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 700px) { .snap-grid { grid-template-columns: 1fr; } }
.snap-section { }
.snap-section h3 { font-size: 10px; text-transform: uppercase; color: var(--fg3); margin-bottom: 6px; letter-spacing: 1px; }
.snap-metric {
  display: flex; justify-content: space-between; padding: 2px 0;
  font-size: 12px; border-bottom: 1px solid var(--bg3);
}
.snap-metric:last-child { border-bottom: none; }
.snap-metric-label { color: var(--fg2); }
.snap-metric-val { color: var(--fg); font-weight: 500; }

/* Window layout */
.win-layout {
  position: relative; background: var(--bg); border: 1px solid var(--bg3);
  border-radius: 6px; height: 150px; overflow: hidden;
}
.win-rect {
  position: absolute; border: 1px solid var(--fg3); border-radius: 3px;
  font-size: 8px; color: var(--fg2); padding: 2px 4px;
  overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  transition: border-color 0.2s;
}
.win-rect.active { border-color: var(--cyan); background: rgba(88,166,255,0.1); }
.win-rect:hover { border-color: var(--yellow); z-index: 10; }

/* Running apps */
.running-apps {
  display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px;
}
.running-app {
  font-size: 10px; padding: 2px 6px; border-radius: 4px;
  background: var(--bg3); color: var(--fg2);
}
.running-app.active-app { background: rgba(88,166,255,0.15); color: var(--cyan); }

/* Loading */
.loading { text-align: center; color: var(--fg3); padding: 40px; }
.empty { text-align: center; color: var(--fg3); padding: 20px; font-size: 12px; }

/* App color dot */
.app-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 2px;
  margin-right: 4px; vertical-align: middle; flex-shrink: 0;
}
</style>
</head>
<body>

@@NAV:explore@@

<!-- Date Nav -->
<div class="date-nav">
  <button onclick="prevDay()" id="btn-prev">&larr;</button>
  <div class="dp" id="dp">
    <button class="dp-trigger" id="dp-trigger" type="button">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15">
        <rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/>
        <line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
      <span id="date-label">—</span>
    </button>
    <div class="dp-pop" id="dp-pop" hidden>
      <div class="dp-head">
        <button class="dp-nav" id="dp-prev" type="button">&lsaquo;</button>
        <span class="dp-title" id="dp-title"></span>
        <button class="dp-nav" id="dp-next" type="button">&rsaquo;</button>
      </div>
      <div class="dp-weekdays"><span>Mo</span><span>Di</span><span>Mi</span>
      <span>Do</span><span>Fr</span><span>Sa</span><span>So</span></div>
      <div class="dp-grid" id="dp-grid"></div>
    </div>
  </div>
  <button onclick="nextDay()" id="btn-next">&rarr;</button>
  <button onclick="goToday()">Heute</button>
</div>

<!-- Day Overview -->
<div class="card" id="overview-card">
  <h2>Tagesübersicht</h2>
  <div class="stats" id="day-stats"></div>
  <div class="dist-grid" id="distributions"></div>
</div>

<!-- Timeline -->
<div class="card" id="timeline-card">
  <h2>Aktivitäts-Timeline</h2>
  <div class="timeline-wrap" id="timeline-wrap">
    <div class="timeline-hours" id="timeline-hours"></div>
    <svg class="timeline-svg" id="timeline-svg" height="70"></svg>
  </div>
</div>

<!-- Sessions -->
<div class="card" id="sessions-card">
  <h2>Sessions</h2>
  <div class="filter-bar" id="filter-bar">
    <input type="text" id="f-text" placeholder="Suche in Titel/Topic/App..." oninput="applyFilters()">
    <select id="f-app" onchange="applyFilters()"><option value="">Alle Apps</option></select>
    <select id="f-proj" onchange="applyFilters()"><option value="">Alle Projekte</option></select>
    <select id="f-topic" onchange="applyFilters()"><option value="">Alle Themen</option></select>
    <button class="sort-btn active" id="sort-time" onclick="sortBy('time')">Zeit</button>
    <button class="sort-btn" id="sort-dur" onclick="sortBy('duration')">Dauer</button>
    <span class="filter-count" id="filter-count"></span>
  </div>
  <div id="sessions-list"></div>
</div>

<!-- Tooltip -->
<div class="timeline-tooltip" id="tooltip"></div>

<script>
const APP_COLORS = [
  '#58a6ff','#3fb950','#d29922','#bc8cff','#f85149',
  '#d18616','#388bfd','#79c0ff','#56d364','#e3b341',
  '#f0883e','#a5d6ff','#7ee787','#d2a8ff','#ff7b72'
];
const appColorMap = {};
let colorIdx = 0;
function appColor(name) {
  if (!appColorMap[name]) appColorMap[name] = APP_COLORS[colorIdx++ % APP_COLORS.length];
  return appColorMap[name];
}

// State
let currentDate = '';
let availableDates = [];
let sessions = [];
let timelineData = [];
let filteredSessions = [];
let expandedIdx = null;
let loadedSnapshots = {};
let sortMode = 'time';

function fmt(sec) {
  if (!sec || sec < 0) return '\u2014';
  sec = Math.round(sec);
  if (sec < 60) return sec + 's';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h > 0 ? h + 'h ' + String(m).padStart(2, '0') + 'm' : m + 'm';
}
function esc(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function pad(n) { return String(n).padStart(2, '0'); }

// --- Init ---
async function init() {
  const r = await fetch('/api/dates');
  availableDates = await r.json();

  // Determine date from URL or use today/latest
  const path = location.pathname;
  const m = path.match(/\/explore\/(\d{4}-\d{2}-\d{2})/);
  if (m && availableDates.includes(m[1])) {
    currentDate = m[1];
  } else if (availableDates.length) {
    const today = new Date().toISOString().slice(0, 10);
    currentDate = availableDates.includes(today) ? today : availableDates[availableDates.length - 1];
  }
  if (currentDate) {
    loadDate(currentDate);
  } else {
    document.getElementById('sessions-list').innerHTML = '<div class="empty">Keine Daten vorhanden</div>';
  }
}

function updateDateUI() {
  const d = new Date(currentDate + 'T00:00:00');
  const days = ['So','Mo','Di','Mi','Do','Fr','Sa'];
  const months = ['Jan','Feb','Mär','Apr','Mai','Jun','Jul','Aug','Sep','Okt','Nov','Dez'];
  document.getElementById('date-label').textContent =
    days[d.getDay()] + ', ' + d.getDate() + '. ' + months[d.getMonth()] + ' ' + d.getFullYear();
  const idx = availableDates.indexOf(currentDate);
  document.getElementById('btn-prev').disabled = idx <= 0;
  document.getElementById('btn-next').disabled = idx >= availableDates.length - 1;
  history.replaceState(null, '', '/explore/' + currentDate);
}

function prevDay() {
  const idx = availableDates.indexOf(currentDate);
  if (idx > 0) loadDate(availableDates[idx - 1]);
}
function nextDay() {
  const idx = availableDates.indexOf(currentDate);
  if (idx < availableDates.length - 1) loadDate(availableDates[idx + 1]);
}
function goToday() {
  const today = new Date().toISOString().slice(0, 10);
  if (availableDates.includes(today)) loadDate(today);
  else if (availableDates.length) loadDate(availableDates[availableDates.length - 1]);
}
// ── Datepicker (wie Screenshots-Seite — nur Tage mit Daten sind klickbar) ──
const DP_MONTHS = ['Januar','Februar','März','April','Mai','Juni','Juli',
                   'August','September','Oktober','November','Dezember'];
let dpY, dpM;   // im Popover angezeigter Monat

function dpRender() {
  const avail = new Set(availableDates);
  document.getElementById('dp-title').textContent = DP_MONTHS[dpM] + ' ' + dpY;
  const first = new Date(dpY, dpM, 1);
  const offset = (first.getDay() + 6) % 7;      // Montag zuerst
  const daysInMonth = new Date(dpY, dpM + 1, 0).getDate();
  let cells = '';
  for (let i = 0; i < offset; i++) cells += '<div class="dp-day dp-empty"></div>';
  const tn = new Date();
  const todayIso = tn.getFullYear() + '-' + pad(tn.getMonth() + 1) + '-' + pad(tn.getDate());
  for (let day = 1; day <= daysInMonth; day++) {
    const iso = dpY + '-' + pad(dpM + 1) + '-' + pad(day);
    const has = avail.has(iso);
    const sel = iso === currentDate;
    const cls = 'dp-day' + (has ? ' has' : ' muted') + (sel ? ' sel' : '')
      + (iso === todayIso ? ' today' : '');
    cells += '<div class="' + cls + '"' + (has ? ' data-d="' + iso + '"' : '') + '>' + day + '</div>';
  }
  const gridEl = document.getElementById('dp-grid');
  gridEl.innerHTML = cells;
  gridEl.querySelectorAll('.dp-day.has').forEach(el =>
    el.addEventListener('click', () => {
      document.getElementById('dp-pop').hidden = true;
      loadDate(el.dataset.d);
    })
  );
  // Navigation außerhalb des verfügbaren Bereichs sperren
  const minIso = availableDates.length ? availableDates[0] : null;
  const maxIso = availableDates.length ? availableDates[availableDates.length - 1] : null;
  document.getElementById('dp-prev').disabled = minIso ? (dpY*12+dpM) <= (+minIso.slice(0,4)*12 + (+minIso.slice(5,7)-1)) : true;
  document.getElementById('dp-next').disabled = maxIso ? (dpY*12+dpM) >= (+maxIso.slice(0,4)*12 + (+maxIso.slice(5,7)-1)) : true;
}

function dpShiftMonth(delta) {
  dpM += delta;
  if (dpM < 0) { dpM = 11; dpY--; }
  else if (dpM > 11) { dpM = 0; dpY++; }
  dpRender();
}

document.getElementById('dp-trigger').addEventListener('click', (e) => {
  e.stopPropagation();
  const pop = document.getElementById('dp-pop');
  if (pop.hidden && currentDate) {
    dpY = +currentDate.slice(0,4); dpM = +currentDate.slice(5,7) - 1;
    dpRender();
  }
  pop.hidden = !pop.hidden;
});
document.getElementById('dp-prev').addEventListener('click', (e) => { e.stopPropagation(); dpShiftMonth(-1); });
document.getElementById('dp-next').addEventListener('click', (e) => { e.stopPropagation(); dpShiftMonth(1); });
document.getElementById('dp-pop').addEventListener('click', (e) => e.stopPropagation());
document.addEventListener('click', () => { document.getElementById('dp-pop').hidden = true; });

async function loadDate(date) {
  currentDate = date;
  expandedIdx = null;
  loadedSnapshots = {};
  colorIdx = 0;
  Object.keys(appColorMap).forEach(k => delete appColorMap[k]);

  updateDateUI();
  document.getElementById('sessions-list').innerHTML =
    '<div class="wt-loading"><div class="wt-spinner"></div><span class="wt-load-label">Sessions laden…</span></div>';
  document.getElementById('day-stats').innerHTML =
    '<div class="wt-loading inline"><div class="wt-spinner sm"></div><span class="wt-load-label">Laden…</span></div>';
  document.getElementById('distributions').innerHTML = '';

  const [sessR, tlR] = await Promise.all([
    fetch('/api/sessions/' + date),
    fetch('/api/snapshots/' + date + '/timeline')
  ]);
  sessions = await sessR.json();
  timelineData = await tlR.json();

  // Assign colors deterministically by total time
  const appTimes = {};
  sessions.forEach(s => {
    appTimes[s.app_name] = (appTimes[s.app_name] || 0) + (s.duration_seconds || 0);
  });
  Object.keys(appTimes).sort((a, b) => appTimes[b] - appTimes[a]).forEach(a => appColor(a));

  renderOverview();
  renderTimeline();
  populateFilters();
  applyFilters();

  document.getElementById('status-text').textContent =
    sessions.length + ' Sessions \u00b7 ' + timelineData.length + ' Snapshots';
}

// --- Overview ---
function renderOverview() {
  if (!sessions.length) {
    document.getElementById('day-stats').innerHTML = '<div class="empty">Keine Sessions</div>';
    document.getElementById('distributions').innerHTML = '';
    return;
  }
  const totalSec = sessions.reduce((s, x) => s + (x.duration_seconds || 0), 0);
  const focus = sessions.filter(s => (s.duration_seconds || 0) >= 1500);
  const focusSec = focus.reduce((s, x) => s + (x.duration_seconds || 0), 0);
  const apps = sessions.map(s => s.app_name);
  const switches = apps.reduce((c, a, i) => i > 0 && a !== apps[i-1] ? c+1 : c, 0);
  const keys = sessions.reduce((s, x) => s + (x.keystrokes_total || 0), 0);
  const clicks = sessions.reduce((s, x) => s + (x.mouse_clicks_total || 0), 0);
  const scrolls = sessions.reduce((s, x) => s + (x.scroll_events_total || 0), 0);
  const clip = sessions.reduce((s, x) => s + (x.clipboard_events || []).length, 0);

  // Topic stats
  const topicSecs = {};
  const topicCount = {};
  const topicProject = {};
  let sessionsWithTopic = 0;
  sessions.forEach(s => {
    const t = (s.topic || '').trim();
    if (!t) return;
    sessionsWithTopic++;
    topicSecs[t] = (topicSecs[t] || 0) + (s.duration_seconds || 0);
    topicCount[t] = (topicCount[t] || 0) + 1;
    if (!topicProject[t]) topicProject[t] = s.project || '';
  });
  const distinctTopics = Object.keys(topicSecs).length;

  document.getElementById('day-stats').innerHTML = [
    ['Aktiv', fmt(totalSec)],
    ['Sessions', sessions.length],
    ['Focus', focus.length + ' (' + fmt(focusSec) + ')'],
    ['Switches', switches],
    ['Keys', keys.toLocaleString()],
    ['Clicks', clicks.toLocaleString()],
    ['Scroll', scrolls.toLocaleString()],
    ['Clipboard', clip + 'x'],
    ['Themen', distinctTopics + ' (' + sessionsWithTopic + ')'],
  ].map(([l,v]) => '<div class="stat"><div class="stat-val">'+v+'</div><div class="stat-label">'+l+'</div></div>').join('');

  // Distributions
  const appDist = {};
  const projDist = {};
  sessions.forEach(s => {
    const a = s.app_name || 'Unknown';
    const p = s.project || 'Other';
    appDist[a] = (appDist[a] || 0) + (s.duration_seconds || 0);
    projDist[p] = (projDist[p] || 0) + (s.duration_seconds || 0);
  });

  // Store dist data globally for search (Topics reuses the proj-coloured dot)
  window._distData = { Apps: appDist, Projekte: projDist, Themen: topicSecs };
  window._topicProject = topicProject;
  window._topicCount = topicCount;

  function distRowHtml(title, name, sec, max) {
    const pct = Math.round(sec / totalSec * 100);
    let c;
    if (title === 'Apps') c = appColor(name);
    else if (title === 'Themen') c = 'var(--cyan)';
    else c = 'var(--blue)';

    // Optional extra suffix (project for topics, count)
    let suffix = '';
    if (title === 'Themen') {
      const proj = window._topicProject ? window._topicProject[name] : '';
      const cnt = window._topicCount ? window._topicCount[name] : 0;
      if (proj) {
        suffix += '<span class="topic-dist-proj" title="Projekt">' + esc(proj) + '</span>';
      }
      if (cnt) {
        suffix += '<span class="topic-dist-count">×' + cnt + '</span>';
      }
    }

    return '<div class="bar-row">'
      + '<span class="app-dot" style="background:'+c+'"></span>'
      + '<div class="bar-name">' + esc(name) + '</div>'
      + suffix
      + '<div class="bar-wrap"><div class="bar-fill" style="width:'+Math.round(sec/max*100)+'%;background:'+c+'"></div></div>'
      + '<div class="bar-pct">' + pct + '%</div>'
      + '<div class="bar-time">' + fmt(sec) + '</div></div>';
  }

  function renderDist(title, dist, query) {
    let sorted = Object.entries(dist).sort((a,b) => b[1]-a[1]);
    const id = 'dist-' + title.replace(/[^a-zA-Z]/g, '');
    const INIT = 15, PAGE = 50;

    // search filter
    if (query) {
      const q = query.toLowerCase();
      sorted = sorted.filter(([n]) => n.toLowerCase().includes(q));
    }

    const max = sorted[0] ? sorted[0][1] : 1;
    let h = '<h2 style="margin-top:0">' + title + ' <span style="font-size:12px;color:var(--fg3);font-weight:400">(' + sorted.length + ')</span></h2>';
    h += '<input type="text" class="dist-search" id="'+id+'-search" placeholder="Suche..." oninput="filterDist(\''+title+'\')" value="'+(query||'')+'">';
    h += '<div id="'+id+'-rows">';

    // first 15
    sorted.slice(0, INIT).forEach(([n, s]) => { h += distRowHtml(title, n, s, max); });

    if (sorted.length > INIT) {
      const distOpen = JSON.parse(localStorage.getItem('wt_expanded_lists') || '[]').includes(id);
      h += '<div id="'+id+'-more" style="display:'+(distOpen?'block':'none')+'">';
      sorted.slice(INIT, PAGE).forEach(([n, s]) => { h += distRowHtml(title, n, s, max); });
      h += '</div>';
      const showN = Math.min(sorted.length, PAGE);
      h += '<a href="#" class="dist-toggle" id="'+id+'-btn" onclick="'
        + 'event.preventDefault();'
        + 'var m=document.getElementById(\''+id+'-more\');'
        + 'var pg=document.getElementById(\''+id+'-pages\');'
        + 'var show=m.style.display===\'none\';'
        + 'm.style.display=show?\'block\':\'none\';'
        + 'if(pg)pg.style.display=show?\'block\':\'none\';'
        + 'var ls=JSON.parse(localStorage.getItem(\'wt_expanded_lists\')||\'[]\');'
        + 'if(show){if(!ls.includes(\''+id+'\'))ls.push(\''+id+'\')}else{ls=ls.filter(function(x){return x!==\''+id+'\'})};'
        + 'localStorage.setItem(\'wt_expanded_lists\',JSON.stringify(ls));'
        + 'this.textContent=show?\'Weniger anzeigen\':\'Alle '+showN+' anzeigen\';'
        + '">'+(distOpen?'Weniger anzeigen':'Alle '+showN+' anzeigen')+'</a>';
    }

    if (sorted.length > PAGE) {
      const distOpen = JSON.parse(localStorage.getItem('wt_expanded_lists') || '[]').includes(id);
      h += '<div id="'+id+'-pages" style="display:'+(distOpen?'block':'none')+'">';
      const pages = Math.ceil((sorted.length - PAGE) / PAGE);
      for (let p = 0; p < pages; p++) {
        const start = PAGE + p * PAGE;
        const end = Math.min(start + PAGE, sorted.length);
        h += '<div id="'+id+'-page-'+p+'" style="display:'+(p===0?'block':'none')+'">';
        sorted.slice(start, end).forEach(([n, s]) => { h += distRowHtml(title, n, s, max); });
        h += '</div>';
      }
      h += '<div class="dist-pagination">';
      for (let p = 0; p < pages; p++) {
        const start = PAGE + p * PAGE;
        if (start >= sorted.length) break;
        const end = Math.min(start + PAGE, sorted.length);
        h += '<a href="#" class="dist-page-btn'+(p===0?' active':'')+'" onclick="'
          + 'event.preventDefault();'
          + 'document.querySelectorAll(\'#'+id+'-pages>div[id]\').forEach(function(el){el.style.display=\'none\'});'
          + 'document.getElementById(\''+id+'-page-'+p+'\').style.display=\'block\';'
          + 'this.parentNode.querySelectorAll(\'.dist-page-btn\').forEach(function(b){b.classList.remove(\'active\')});'
          + 'this.classList.add(\'active\');'
          + '">'+(start+1)+'–'+end+'</a>';
      }
      h += '</div></div>';
    }

    h += '</div>';
    return h;
  }

  window.filterDist = function(title) {
    const id = 'dist-' + title.replace(/[^a-zA-Z]/g, '');
    const q = document.getElementById(id+'-search').value;
    const card = document.getElementById(id+'-search').closest('.dist-card');
    card.innerHTML = renderDist(title, window._distData[title], q);
    // restore focus to search input
    const inp = document.getElementById(id+'-search');
    if (inp) { inp.focus(); inp.selectionStart = inp.selectionEnd = inp.value.length; }
  };

  let distHtml =
    '<div class="dist-card">' + renderDist('Apps', appDist, '') + '</div>'
    + '<div class="dist-card">' + renderDist('Projekte', projDist, '') + '</div>';
  if (distinctTopics > 0) {
    distHtml += '<div class="dist-card">' + renderDist('Themen', topicSecs, '') + '</div>';
  }
  document.getElementById('distributions').innerHTML = distHtml;
}

// --- Timeline ---
function renderTimeline() {
  // Hour labels
  let hh = '';
  for (let h = 0; h < 24; h += 2) hh += '<span>'+pad(h)+':00</span>';
  document.getElementById('timeline-hours').innerHTML = hh;

  const svg = document.getElementById('timeline-svg');
  const w = svg.getBoundingClientRect().width || 1200;
  svg.setAttribute('viewBox', '0 0 ' + w + ' 70');
  svg.innerHTML = '';

  if (!sessions.length) return;

  // Day boundaries
  const dayStart = new Date(currentDate + 'T00:00:00').getTime();
  const dayEnd = dayStart + 86400000;
  const scale = w / 86400000;

  // Grid lines
  for (let h = 0; h < 24; h++) {
    const x = h * 3600000 * scale;
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', x); line.setAttribute('x2', x);
    line.setAttribute('y1', 0); line.setAttribute('y2', 70);
    line.setAttribute('stroke', 'var(--bg3)'); line.setAttribute('stroke-width', '1');
    svg.appendChild(line);
  }

  // Session blocks
  sessions.forEach((s, i) => {
    const start = new Date(s.start).getTime();
    const end = new Date(s.end).getTime();
    const x = Math.max(0, (start - dayStart) * scale);
    const rw = Math.max(2, (end - start) * scale);
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', x);
    rect.setAttribute('y', 2);
    rect.setAttribute('width', rw);
    rect.setAttribute('height', 28);
    rect.setAttribute('rx', 3);
    rect.setAttribute('fill', appColor(s.app_name));
    rect.setAttribute('opacity', '0.8');
    rect.setAttribute('data-idx', i);
    rect.style.cursor = 'pointer';
    rect.addEventListener('mouseenter', e => showTooltip(e, s));
    rect.addEventListener('mouseleave', hideTooltip);
    rect.addEventListener('click', () => scrollToSession(i));
    svg.appendChild(rect);
  });

  // Input sparkline from timeline data
  if (timelineData.length > 1) {
    // Aggregate by minute
    const minutes = new Array(1440).fill(0);
    timelineData.forEach(s => {
      const t = new Date(s.ts).getTime();
      const min = Math.floor((t - dayStart) / 60000);
      if (min >= 0 && min < 1440) {
        minutes[min] += (s.keys || 0) + (s.clicks || 0);
      }
    });
    const maxInput = Math.max(...minutes, 1);

    let pathD = '';
    for (let m = 0; m < 1440; m++) {
      if (minutes[m] === 0) continue;
      const x = (m * 60000) * scale;
      const h = Math.max(1, (minutes[m] / maxInput) * 25);
      const y = 68 - h;
      pathD += 'M' + x.toFixed(1) + ',' + 68 + 'V' + y.toFixed(1) + ' ';
    }
    if (pathD) {
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', pathD);
      path.setAttribute('stroke', '#3fb950');
      path.setAttribute('stroke-width', '1');
      path.setAttribute('opacity', '0.5');
      path.setAttribute('fill', 'none');
      svg.appendChild(path);
    }
  }
}

const tooltip = document.getElementById('tooltip');
function showTooltip(e, s) {
  const start = new Date(s.start);
  const end = new Date(s.end);
  tooltip.innerHTML = '<strong>' + esc(s.app_name) + '</strong><br>'
    + esc(s.window_title || '\u2014') + '<br>'
    + pad(start.getHours()) + ':' + pad(start.getMinutes()) + ' \u2013 '
    + pad(end.getHours()) + ':' + pad(end.getMinutes()) + '<br>'
    + fmt(s.duration_seconds) + ' \u00b7 ' + (s.project || 'Other');
  tooltip.style.display = 'block';
  tooltip.style.left = (e.clientX + 12) + 'px';
  tooltip.style.top = (e.clientY + 12) + 'px';
}
function hideTooltip() { tooltip.style.display = 'none'; }

function scrollToSession(idx) {
  const el = document.getElementById('sess-' + idx);
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    toggleSession(idx);
  }
}

// --- Filters ---
function populateFilters() {
  const apps = [...new Set(sessions.map(s => s.app_name))].sort();
  const projs = [...new Set(sessions.map(s => s.project || 'Other'))].sort();
  const topics = [...new Set(sessions.map(s => (s.topic || '').trim()).filter(Boolean))].sort();
  let ao = '<option value="">Alle Apps</option>';
  apps.forEach(a => ao += '<option>' + esc(a) + '</option>');
  document.getElementById('f-app').innerHTML = ao;
  let po = '<option value="">Alle Projekte</option>';
  projs.forEach(p => po += '<option>' + esc(p) + '</option>');
  document.getElementById('f-proj').innerHTML = po;
  let to = '<option value="">Alle Themen (' + topics.length + ')</option>';
  topics.forEach(t => to += '<option>' + esc(t) + '</option>');
  document.getElementById('f-topic').innerHTML = to;
}

function applyFilters() {
  sessActivePage = 0;
  const text = document.getElementById('f-text').value.toLowerCase();
  const app = document.getElementById('f-app').value;
  const proj = document.getElementById('f-proj').value;
  const topic = document.getElementById('f-topic').value;

  filteredSessions = sessions.filter((s, i) => {
    s._origIdx = i;
    if (app && s.app_name !== app) return false;
    if (proj && (s.project || 'Other') !== proj) return false;
    if (topic && (s.topic || '') !== topic) return false;
    if (text) {
      const hay = ((s.app_name || '') + ' ' + (s.window_title || '') + ' ' + (s.project || '') + ' ' + (s.topic || '')).toLowerCase();
      if (!hay.includes(text)) return false;
    }
    return true;
  });

  if (sortMode === 'duration') {
    filteredSessions.sort((a, b) => (b.duration_seconds || 0) - (a.duration_seconds || 0));
  }

  document.getElementById('filter-count').textContent =
    filteredSessions.length + ' / ' + sessions.length;

  renderSessions();
}

function sortBy(mode) {
  sortMode = mode;
  document.getElementById('sort-time').classList.toggle('active', mode === 'time');
  document.getElementById('sort-dur').classList.toggle('active', mode === 'duration');
  applyFilters();
}

// --- Sessions List ---
let sessShowAll = localStorage.getItem('wt_sess_expanded') === 'true';
let sessActivePage = 0;
const SESS_INIT = 15, SESS_PAGE = 50;

let _sessMaxDur = 1;

function intBars(val) {
  const iv = Math.round(Math.min(val || 0, 10));
  const col = iv <= 3 ? 'var(--cyan)' : iv <= 6 ? 'var(--yellow)' : 'var(--red)';
  let b = '';
  for (let i = 0; i < 5; i++) {
    const on = iv > i * 2;
    const h = 3 + i * 2.5;
    b += '<div class="sess-int-seg" style="height:'+h+'px;background:'+(on ? col : 'var(--bg3)')+'"></div>';
  }
  return b;
}

function scrUrlFromPath(p) {
  if (!p) return '';
  const parts = String(p).split('/');
  if (parts.length < 2) return '';
  return '/screenshots/file/' + parts[parts.length - 2] + '/' + parts[parts.length - 1];
}

function sessRowHtml(s) {
  const i = s._origIdx;
  const start = new Date(s.start);
  const t = pad(start.getHours()) + ':' + pad(start.getMinutes());
  const c = appColor(s.app_name);
  const expanded = expandedIdx === i;
  const durPct = Math.round((s.duration_seconds || 0) / _sessMaxDur * 100);
  const iv = Math.round(Math.min(s.intensity_score || 0, 10));
  const topicHtml = s.topic ? esc(s.topic) : '';
  const toolBadge = s.is_tool_app ? '<span class="sess-tool" title="Tool-App">◎</span>' : '';
  const scrPaths = s.screenshot_paths || [];
  const scrN = scrPaths.length;
  const scrUrl = scrN ? scrUrlFromPath(scrPaths[0]) : '';
  const scrHtml = scrN && scrUrl
    ? '<a class="sess-scr" href="'+esc(scrUrl)+'" target="_blank" onclick="event.stopPropagation()" title="'+scrN+' Screenshot'+(scrN>1?'s':'')+'">📷 '+scrN+'</a>'
    : '<span class="sess-scr-empty"></span>';
  let h = '<div class="sess-row' + (expanded ? ' expanded' : '') + '" id="sess-' + i + '" onclick="toggleSession(' + i + ')">'
    + '<span class="sess-arrow">\u25B6</span>'
    + '<div class="sess-time">' + t + '</div>'
    + '<span class="app-dot" style="background:'+c+'"></span>'
    + '<div class="sess-app">' + esc(s.app_name) + toolBadge + '</div>'
    + '<div class="sess-title">' + esc(s.window_title || '\u2014') + '</div>'
    + '<div class="sess-topic' + (s.topic_long ? ' has-long' : '') + '"'
    + (s.topic_long ? ' title="' + esc(s.topic_long).replace(/"/g, '&quot;') + '"' : '') + '>' + topicHtml + '</div>'
    + '<div class="sess-proj">' + esc(s.project || '') + '</div>'
    + '<div class="sess-dur-wrap"><div class="sess-dur">' + fmt(s.duration_seconds) + '</div>'
    + '<div class="sess-dur-bar"><div class="sess-dur-fill" style="width:'+durPct+'%"></div></div></div>'
    + '<div class="sess-int-wrap">' + intBars(s.intensity_score) + '</div>'
    + scrHtml
    + '<div class="sess-snaps">' + (s.snapshot_count || 0) + '</div>'
    + '</div>';
  h += '<div class="sess-detail' + (expanded ? ' show' : '') + '" id="detail-' + i + '">'
    + renderDetail(s, i) + '</div>';
  return h;
}

function renderSessions() {
  if (!filteredSessions.length) {
    document.getElementById('sessions-list').innerHTML = '<div class="empty">Keine Sessions gefunden</div>';
    return;
  }

  _sessMaxDur = Math.max(...filteredSessions.map(s => s.duration_seconds || 0), 1);
  const total = filteredSessions.length;
  let h = '';

  // first 15
  filteredSessions.slice(0, SESS_INIT).forEach(s => { h += sessRowHtml(s); });

  if (total > SESS_INIT) {
    h += '<div id="sess-more" style="display:' + (sessShowAll ? 'block' : 'none') + '">';
    filteredSessions.slice(SESS_INIT, SESS_PAGE).forEach(s => { h += sessRowHtml(s); });
    h += '</div>';

    if (total > SESS_PAGE) {
      h += '<div id="sess-pages" style="display:' + (sessShowAll ? 'block' : 'none') + '">';
      const pages = Math.ceil((total - SESS_PAGE) / SESS_PAGE);
      for (let p = 0; p < pages; p++) {
        const start = SESS_PAGE + p * SESS_PAGE;
        const end = Math.min(start + SESS_PAGE, total);
        h += '<div id="sess-page-' + p + '" style="display:' + (p === sessActivePage ? 'block' : 'none') + '">';
        filteredSessions.slice(start, end).forEach(s => { h += sessRowHtml(s); });
        h += '</div>';
      }
      h += '<div class="dist-pagination">';
      for (let p = 0; p < pages; p++) {
        const start = SESS_PAGE + p * SESS_PAGE;
        if (start >= total) break;
        const end = Math.min(start + SESS_PAGE, total);
        h += '<a href="#" class="dist-page-btn' + (p === sessActivePage ? ' active' : '') + '" onclick="'
          + 'event.preventDefault();event.stopPropagation();'
          + 'sessActivePage='+p+';'
          + 'document.querySelectorAll(\'#sess-pages>div[id]\').forEach(function(el){el.style.display=\'none\'});'
          + 'document.getElementById(\'sess-page-'+p+'\').style.display=\'block\';'
          + 'this.parentNode.querySelectorAll(\'.dist-page-btn\').forEach(function(b){b.classList.remove(\'active\')});'
          + 'this.classList.add(\'active\');'
          + '">' + (start + 1) + '\u2013' + end + '</a>';
      }
      h += '</div></div>';
    }

    const showN = Math.min(total, SESS_PAGE);
    h += '<a href="#" class="dist-toggle" id="sess-toggle" onclick="'
      + 'event.preventDefault();'
      + 'sessShowAll=!sessShowAll;localStorage.setItem(\'wt_sess_expanded\',sessShowAll);'
      + 'var m=document.getElementById(\'sess-more\');'
      + 'var pg=document.getElementById(\'sess-pages\');'
      + 'if(m)m.style.display=sessShowAll?\'block\':\'none\';'
      + 'if(pg)pg.style.display=sessShowAll?\'block\':\'none\';'
      + 'this.textContent=sessShowAll?\'Weniger anzeigen\':\'Alle '+showN+' anzeigen\';'
      + '">' + (sessShowAll ? 'Weniger anzeigen' : 'Alle ' + showN + ' anzeigen') + '</a>';
  }

  document.getElementById('sessions-list').innerHTML = h;
}

function renderDetail(s, idx) {
  let h = '<div class="detail-grid">';

  // Left column
  h += '<div>';
  if (s.window_title) h += '<div class="detail-field"><div class="detail-label">Fenstertitel</div><div class="detail-val">' + esc(s.window_title) + '</div></div>';
  if (s.topic) h += '<div class="detail-field"><div class="detail-label">Thema</div><div class="detail-val" style="color:var(--cyan)">' + esc(s.topic) + '</div></div>';
  if (s.topic_long) h += '<div class="detail-field"><div class="detail-label">Beschreibung</div><div class="detail-val topic-long-val">' + esc(s.topic_long) + '</div></div>';
  if (s.project) h += '<div class="detail-field"><div class="detail-label">Projekt</div><div class="detail-val" style="color:var(--blue)">' + esc(s.project) + (s.category ? ' <span style="color:var(--fg3);font-size:11px">· ' + esc(s.category) + '</span>' : '') + '</div></div>';
  if (s.match_reason) h += '<div class="detail-field"><div class="detail-label">Match-Grund</div><div class="detail-val" style="font-family:monospace;font-size:11px;color:var(--fg2)">' + esc(s.match_reason) + '</div></div>';
  if (s.url) h += '<div class="detail-field"><div class="detail-label">URL</div><div class="detail-val">' + esc(s.url) + '</div></div>';
  if (s.git_repo) h += '<div class="detail-field"><div class="detail-label">Git</div><div class="detail-val">' + esc(s.git_repo) + ' / ' + esc(s.git_branch || '\u2014') + '</div></div>';
  if (s.calendar_event) h += '<div class="detail-field"><div class="detail-label">Kalender</div><div class="detail-val">' + esc(s.calendar_event) + '</div></div>';
  if (s.app_category) h += '<div class="detail-field"><div class="detail-label">Kategorie</div><div class="detail-val">' + esc(s.app_category) + '</div></div>';
  if (s.parallel_media) h += '<div class="detail-field"><div class="detail-label">Media</div><div class="detail-val">\u266B ' + esc(typeof s.parallel_media === 'string' ? s.parallel_media : JSON.stringify(s.parallel_media)) + '</div></div>';

  // Clipboard
  const clips = s.clipboard_events || [];
  if (clips.length) {
    h += '<div class="detail-field"><div class="detail-label">Clipboard (' + clips.length + ')</div><div class="detail-val">';
    clips.forEach(c => {
      h += '<span style="color:var(--fg2);font-size:11px">' + esc(c.source_app || '') + ' \u2014 ' + esc(c.type || '') + ' (' + (c.length || 0) + ' chars)</span><br>';
    });
    h += '</div></div>';
  }
  h += '</div>';

  // Right column — input bars
  h += '<div>';
  const maxInput = Math.max(s.keystrokes_total || 0, s.mouse_clicks_total || 0, s.scroll_events_total || 0, 1);
  h += '<div class="detail-field"><div class="detail-label">Dauer</div><div class="detail-val">' + fmt(s.duration_seconds)
    + ' (' + (s.snapshot_count || 0) + ' Snapshots)</div></div>';
  h += '<div class="input-bars">';
  h += inputBar('Keys', s.keystrokes_total || 0, maxInput, 'var(--green)');
  h += inputBar('Clicks', s.mouse_clicks_total || 0, maxInput, 'var(--cyan)');
  h += inputBar('Scroll', s.scroll_events_total || 0, maxInput, 'var(--purple)');
  h += '</div>';
  h += '<div class="detail-field" style="margin-top:8px"><div class="detail-label">Intensität</div><div class="detail-val" style="color:var(--yellow)">' + (s.intensity_score || 0) + ' / 10</div></div>';
  h += '</div>';

  h += '</div>'; // detail-grid

  // Snapshot button
  h += '<button class="snap-btn" onclick="event.stopPropagation(); loadSnapshots(' + idx + ')" id="snap-btn-' + idx + '">'
    + '\u25BC Snapshots anzeigen (' + (s.snapshot_count || 0) + ')</button>';
  h += '<div class="snap-panel" id="snap-panel-' + idx + '"></div>';

  return h;
}

function inputBar(label, val, max, color) {
  const pct = Math.round(val / max * 100);
  return '<div class="input-bar">'
    + '<div class="input-bar-label">' + label + '</div>'
    + '<div class="input-bar-track"><div class="input-bar-fill" style="width:'+pct+'%;background:'+color+'"></div></div>'
    + '<div class="input-bar-val">' + val.toLocaleString() + '</div></div>';
}

function toggleSession(idx) {
  const prevIdx = expandedIdx;
  if (expandedIdx === idx) {
    expandedIdx = null;
  } else {
    expandedIdx = idx;
  }

  // Close previous via DOM
  if (prevIdx !== null) {
    const prevRow = document.getElementById('sess-' + prevIdx);
    const prevDetail = document.getElementById('detail-' + prevIdx);
    if (prevRow) prevRow.classList.remove('expanded');
    if (prevDetail) prevDetail.classList.remove('show');
  }

  // Open new via DOM
  if (expandedIdx !== null) {
    const row = document.getElementById('sess-' + expandedIdx);
    const detail = document.getElementById('detail-' + expandedIdx);
    if (row) row.classList.add('expanded');
    if (detail) {
      detail.classList.add('show');
      detail.innerHTML = renderDetail(sessions[expandedIdx], expandedIdx);
    }
  }
}

// --- Snapshots ---
async function loadSnapshots(idx) {
  const s = sessions[idx];
  const panel = document.getElementById('snap-panel-' + idx);
  const btn = document.getElementById('snap-btn-' + idx);

  if (loadedSnapshots[idx]) {
    // Toggle visibility
    if (panel.style.display === 'none') {
      panel.style.display = '';
      btn.textContent = '\u25B2 Snapshots verbergen';
    } else {
      panel.style.display = 'none';
      btn.textContent = '\u25BC Snapshots anzeigen (' + (s.snapshot_count || 0) + ')';
    }
    return;
  }

  btn.innerHTML = '<span class="wt-spinner sm"></span>Laden…';
  btn.disabled = true;

  try {
    const r = await fetch('/api/snapshots/' + currentDate + '/range?start=' + encodeURIComponent(s.start) + '&end=' + encodeURIComponent(s.end));
    const snaps = await r.json();
    loadedSnapshots[idx] = snaps;

    btn.textContent = '\u25B2 Snapshots verbergen';
    btn.disabled = false;
    renderSnapshotPanel(idx, snaps, 0);
  } catch(e) {
    btn.textContent = 'Fehler beim Laden';
    btn.disabled = false;
  }
}

function renderSnapshotPanel(idx, snaps, activeIdx) {
  const panel = document.getElementById('snap-panel-' + idx);
  if (!snaps.length) {
    panel.innerHTML = '<div class="empty">Keine Snapshots in diesem Zeitraum</div>';
    return;
  }

  let h = '<div class="snap-scrubber">';
  snaps.forEach((s, i) => {
    const inp = s.input || {};
    const hasInput = (inp.keystrokes || 0) + (inp.mouse_clicks_left || 0) > 0;
    const cls = (i === activeIdx ? ' active' : '') + (hasInput ? ' has-input' : '');
    const ts = new Date(s.ts);
    h += '<div class="snap-dot' + cls + '" onclick="event.stopPropagation(); showSnap(' + idx + ',' + i + ')" title="' + pad(ts.getHours()) + ':' + pad(ts.getMinutes()) + ':' + pad(ts.getSeconds()) + '"></div>';
  });
  h += '</div>';

  h += renderSnapshotDetail(snaps[activeIdx]);
  panel.innerHTML = h;
}

function showSnap(idx, snapIdx) {
  renderSnapshotPanel(idx, loadedSnapshots[idx], snapIdx);
}

function renderSnapshotDetail(snap) {
  const ts = new Date(snap.ts);
  const aa = snap.active_app || {};
  const inp = snap.input || {};
  const sys = snap.system || {};

  let h = '<div class="snap-detail-card">';

  // Header
  h += '<div class="snap-header">'
    + '<span class="snap-ts">' + pad(ts.getHours()) + ':' + pad(ts.getMinutes()) + ':' + pad(ts.getSeconds()) + '</span>'
    + '<span class="snap-app-info"><span class="app-dot" style="background:' + appColor(aa.name || '') + '"></span>' + esc(aa.name || '\u2014') + '</span>'
    + '</div>';

  h += '<div class="snap-grid">';

  // Left: Metrics + System
  h += '<div>';

  // Input
  h += '<div class="snap-section"><h3>Input</h3>';
  h += metric('Tastenanschläge', inp.keystrokes || 0);
  h += metric('Mausklicks (L/R)', (inp.mouse_clicks_left || 0) + ' / ' + (inp.mouse_clicks_right || 0));
  h += metric('Scroll-Events', inp.scroll_events || 0);
  h += metric('Mausdistanz', Math.round(inp.mouse_distance_px || 0) + ' px');
  h += metric('Idle Tastatur', Math.round(inp.idle_seconds_keyboard || 0) + 's');
  h += metric('Idle Maus', Math.round(inp.idle_seconds_mouse || 0) + 's');
  if (inp.mouse_position) h += metric('Mausposition', inp.mouse_position.x + ', ' + inp.mouse_position.y);
  h += '</div>';

  // System
  h += '<div class="snap-section" style="margin-top:10px"><h3>System</h3>';
  if (sys.active_space != null) h += metric('Space', sys.active_space);
  if (sys.battery_pct != null) h += metric('Akku', sys.battery_pct + '%' + (sys.battery_charging ? ' \u26A1' : ''));
  if (sys.brightness != null) h += metric('Helligkeit', Math.round(sys.brightness * 100) + '%');
  h += '</div>';

  // Clipboard
  const clip = snap.clipboard || {};
  if (clip.changed) {
    h += '<div class="snap-section" style="margin-top:10px"><h3>Clipboard</h3>';
    h += metric('Quelle', clip.source_app || '\u2014');
    h += metric('Typ', clip.type || '\u2014');
    h += metric('Länge', clip.length || 0);
    h += '</div>';
  }

  // Media
  if (snap.media && snap.media.title) {
    h += '<div class="snap-section" style="margin-top:10px"><h3>Media</h3>';
    h += metric('Titel', snap.media.title);
    if (snap.media.artist) h += metric('Künstler', snap.media.artist);
    if (snap.media.service) h += metric('Service', snap.media.service);
    h += '</div>';
  }

  // Git
  if (snap.git && snap.git.repo) {
    h += '<div class="snap-section" style="margin-top:10px"><h3>Git</h3>';
    h += metric('Repo', snap.git.repo);
    h += metric('Branch', snap.git.branch || '\u2014');
    if (snap.git.recent_commits_count) h += metric('Recent Commits', snap.git.recent_commits_count);
    h += '</div>';
  }

  // Calendar
  if (snap.calendar && snap.calendar.in_meeting) {
    h += '<div class="snap-section" style="margin-top:10px"><h3>Kalender</h3>';
    h += metric('Event', snap.calendar.event_title || '\u2014');
    h += metric('Kalender', snap.calendar.event_calendar || '\u2014');
    if (snap.calendar.attendee_count) h += metric('Teilnehmer', snap.calendar.attendee_count);
    h += '</div>';
  }

  h += '</div>';

  // Right: Window Layout + Running Apps
  h += '<div>';

  // Window title
  if (aa.window_title) {
    h += '<div class="snap-section"><h3>Fenstertitel</h3>';
    h += '<div style="font-size:12px;color:var(--fg);word-break:break-all">' + esc(aa.window_title) + '</div></div>';
  }

  // Window layout
  const wins = snap.visible_windows || [];
  if (wins.length) {
    h += '<div class="snap-section" style="margin-top:10px"><h3>Sichtbare Fenster (' + wins.length + ')</h3>';
    h += renderWindowLayout(wins);
    h += '</div>';
  }

  // Running apps
  const running = snap.running_apps || [];
  if (running.length) {
    h += '<div class="snap-section" style="margin-top:10px"><h3>Laufende Apps (' + running.length + ')</h3>';
    h += '<div class="running-apps">';
    running.forEach(app => {
      const cls = app.active ? ' active-app' : '';
      h += '<span class="running-app' + cls + '">' + esc(app.name) + '</span>';
    });
    h += '</div></div>';
  }

  h += '</div>';
  h += '</div>'; // snap-grid
  h += '</div>'; // snap-detail-card
  return h;
}

function metric(label, val) {
  return '<div class="snap-metric"><span class="snap-metric-label">' + label + '</span><span class="snap-metric-val">' + esc(String(val)) + '</span></div>';
}

function renderWindowLayout(wins) {
  // Find screen bounds
  let maxW = 0, maxH = 0;
  wins.forEach(w => {
    const r = (w.position ? w.position.x : 0) + (w.size ? w.size.w : 100);
    const b = (w.position ? w.position.y : 0) + (w.size ? w.size.h : 100);
    if (r > maxW) maxW = r;
    if (b > maxH) maxH = b;
  });
  if (!maxW) maxW = 1920;
  if (!maxH) maxH = 1080;

  const layoutH = 150;
  const scale = Math.min(1, layoutH / maxH);
  const layoutW = maxW * scale;

  let h = '<div class="win-layout" style="height:'+layoutH+'px;width:100%;max-width:'+Math.round(layoutW)+'px">';
  wins.forEach(w => {
    const x = ((w.position ? w.position.x : 0) / maxW * 100);
    const y = ((w.position ? w.position.y : 0) / maxH * 100);
    const ww = ((w.size ? w.size.w : 100) / maxW * 100);
    const hh = ((w.size ? w.size.h : 100) / maxH * 100);
    const cls = w.is_active ? ' active' : '';
    h += '<div class="win-rect' + cls + '" style="left:'+x.toFixed(1)+'%;top:'+y.toFixed(1)+'%;width:'+ww.toFixed(1)+'%;height:'+hh.toFixed(1)+'%" title="' + esc(w.app) + ': ' + esc(w.title) + '">'
      + esc(w.app || '') + '</div>';
  });
  h += '</div>';
  return h;
}

// Resize timeline on window resize
window.addEventListener('resize', () => { if (sessions.length) renderTimeline(); });

init();
</script>
</body>
</html>"""


STATS_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkTracker Statistics</title>
<script defer src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
:root {
  /* Palette mirrors docs/index.html — acid-green accent, slate-blue cyan */
  --bg: #0c1116; --bg2: #141a20; --bg3: #1b222a;
  --fg: #cfd7e0; --fg2: #8a95a3; --fg3: #484f58;
  --cyan: #4fc3d8; --green: #6fe28a; --yellow: #d29922; --acid: #d4f500;
  --red: #ff4d4f; --purple: #b392ff; --blue: #4fc3d8;
  --border: #2b3642; --white: #f4f8ff;
  --orange: #d18616;
  --card-top: #161e26; --card-hover: #3a4754;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  background: var(--bg); color: var(--fg);
  font-size: 13px; padding: 16px clamp(16px, 2.5vw, 36px) 48px; max-width: 1760px; margin: 0 auto;
}
a { color: var(--cyan); text-decoration: none; }
a:hover { text-decoration: underline; }

.header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 14px; padding-bottom: 10px;
  border-bottom: 1px solid var(--bg3);
}
.header h1 { font-size: 18px; font-weight: 600; color: var(--fg); }
.header-right { color: var(--fg2); font-size: 12px; }

.card {
  background: linear-gradient(180deg, var(--card-top) 0%, var(--bg2) 55%);
  border: 1px solid var(--border);
  border-radius: 14px; padding: 16px; margin-bottom: 14px;
  box-shadow: 0 1px 2px rgba(0,0,0,.25), 0 14px 30px -24px rgba(0,0,0,.6);
  transition: border-color 0.15s, box-shadow 0.2s;
}
.card:hover { border-color: var(--card-hover);
  box-shadow: 0 2px 4px rgba(0,0,0,.25), 0 18px 36px -22px rgba(0,0,0,.65); }

/* Controls */
.controls { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; }
.controls .group { display: flex; gap: 6px; align-items: center; }
.btn {
  background: var(--bg3); color: var(--fg); border: 1px solid var(--bg3);
  border-radius: 4px; padding: 5px 12px; font-size: 12px; cursor: pointer;
  font-family: inherit;
}
.btn:hover { background: var(--blue); color: var(--bg); border-color: var(--blue); }
.btn.active { background: var(--cyan); color: var(--bg); border-color: var(--cyan); }
.btn.primary { background: var(--cyan); color: var(--bg); border-color: var(--cyan); }
.btn.primary:hover { background: var(--blue); border-color: var(--blue); }
.controls label { color: var(--fg2); font-size: 12px; display: flex; gap: 5px; align-items: center; }
.controls input[type=date] {
  background: var(--bg); color: var(--fg); border: 1px solid var(--bg3);
  border-radius: 4px; padding: 4px 7px; font-family: inherit; font-size: 12px;
}
.controls input[type=checkbox] { accent-color: var(--cyan); }
.controls select {
  background: var(--bg); color: var(--fg); border: 1px solid var(--bg3);
  border-radius: 4px; padding: 4px 7px; font-family: inherit; font-size: 12px;
}
.summary {
  color: var(--fg2); font-size: 12px; margin-top: 10px;
  padding-top: 10px; border-top: 1px solid var(--bg3);
}
.summary strong { color: var(--fg); }

/* Tabs */
.tab-switcher {
  display: flex; gap: 2px;
  border-bottom: 1px solid var(--bg3);
}
.tab-btn {
  background: transparent; color: var(--fg2); border: none;
  border-bottom: 2px solid transparent;
  padding: 10px 18px; font-size: 13px; cursor: pointer;
  font-family: inherit;
}
.tab-btn:hover { color: var(--fg); }
.tab-btn.active {
  color: var(--cyan); border-bottom-color: var(--cyan);
}
.tab-panel { display: none; padding-top: 14px; }
.tab-panel.active { display: block; }

/* Crossfilter */
.selection-bar {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 2px 12px; margin-bottom: 10px;
  border-bottom: 1px solid var(--bg3);
  font-size: 12px; color: var(--fg2);
}
.selection-bar .spacer { flex: 1; }
.cf-cols {
  display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px;
}
@media (max-width: 900px) { .cf-cols { grid-template-columns: 1fr; } }
.cf-col h3 {
  font-size: 11px; color: var(--fg2); text-transform: uppercase;
  letter-spacing: 0.5px; margin-bottom: 8px; padding-bottom: 6px;
  border-bottom: 1px solid var(--bg3);
  display: flex; justify-content: space-between; align-items: center;
  font-weight: 600;
}
.cf-col h3 .count { color: var(--fg3); font-weight: 400; font-size: 10px; }
.cf-rows { max-height: 560px; overflow-y: auto; padding-right: 4px; }
.cf-rows::-webkit-scrollbar { width: 6px; }
.cf-rows::-webkit-scrollbar-thumb { background: var(--bg3); border-radius: 3px; }
.cf-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 7px; border-radius: 4px;
  cursor: pointer; transition: background 0.12s;
  font-size: 12px;
}
.cf-row:hover { background: var(--bg3); }
.cf-row.selected {
  background: rgba(88, 166, 255, 0.14);
  outline: 1px solid var(--cyan);
}
.cf-name {
  flex: 1; color: var(--fg); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; min-width: 0;
}
.cf-bar-wrap {
  width: 60px; flex-shrink: 0; height: 6px;
  background: var(--bg3); border-radius: 3px; overflow: hidden;
}
.cf-bar { height: 100%; border-radius: 3px; transition: width 0.3s; }
.cf-col.col-topic .cf-bar { background: var(--col-topic, var(--cyan)); }
.cf-col.col-project .cf-bar { background: var(--col-project, var(--purple)); }
.cf-col.col-app .cf-bar { background: var(--col-app, var(--green)); }
.cf-time {
  width: 58px; text-align: right; color: var(--yellow);
  font-size: 11px; flex-shrink: 0;
}
.cf-count {
  width: 26px; text-align: right; color: var(--fg3);
  font-size: 11px; flex-shrink: 0;
}
.cf-empty { color: var(--fg3); padding: 10px 0; font-style: italic; text-align: center; }

/* Kategorie-Pools */
.cat-pools { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
.cat-pool {
  background: var(--bg2); border: 1px solid var(--bg3); border-radius: 10px;
  border-left: 3px solid var(--bg3); padding: 12px 14px;
}
.cat-pool.selected { outline: 1px solid var(--cyan); }
.cat-pool-head {
  display: flex; align-items: center; gap: 9px; cursor: pointer;
  padding-bottom: 9px; margin-bottom: 9px; border-bottom: 1px solid var(--bg3);
}
.cat-pool-head:hover .cat-pool-name { color: var(--white); }
.cat-pool-dot { width: 11px; height: 11px; border-radius: 50%; flex: 0 0 auto; }
.cat-pool-name { flex: 1; font-weight: 600; font-size: 14px; color: var(--fg);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.cat-pool-time { color: var(--yellow); font-size: 12px; font-variant-numeric: tabular-nums; }
.cat-pool-meta { color: var(--fg3); font-size: 10.5px; margin-left: 7px; }
.cat-pool-projs { display: flex; flex-direction: column; gap: 3px; }
.cat-prow {
  display: flex; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 4px;
  cursor: pointer; font-size: 12px; transition: background 0.12s;
}
.cat-prow:hover { background: var(--bg3); }
.cat-prow.selected { background: rgba(88,166,255,0.14); outline: 1px solid var(--cyan); }
.cat-prow .cf-name { flex: 1; color: var(--fg); }
.cat-prow .cf-bar-wrap { width: 54px; flex-shrink: 0; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.cat-prow .cf-bar { height: 100%; border-radius: 3px; transition: width .3s; }
.cat-prow .cf-time { width: 56px; text-align: right; color: var(--yellow); font-size: 11px; flex-shrink: 0; }
.cat-prow .cf-count { width: 26px; text-align: right; color: var(--fg3); font-size: 11px; flex-shrink: 0; }

/* Viz */
.matrix-controls {
  display: flex; gap: 16px; margin-bottom: 12px;
  padding-bottom: 10px; border-bottom: 1px solid var(--bg3);
  flex-wrap: wrap;
}
.chart-box { height: min(1100px, calc(100vh - 180px)); min-height: 760px; width: 100%; }
/* Sankey-Höhe wird per JS an die Knotenanzahl angepasst (sizeSankeyContainer) */
#sankey-chart { height: 960px; min-height: 600px; }
.color-controls {
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  margin-bottom: 10px; padding-bottom: 10px; border-bottom: 1px solid var(--bg3);
  position: relative;
}
.cc-label { color: var(--fg3); font-size: 12px; margin-right: 2px; }
/* Moderne Farbwahl: Swatch-Chip öffnet ein Colorwheel-Popover */
.cw-swatch {
  display: inline-flex; align-items: center; gap: 7px; font-size: 12px; color: var(--fg2);
  background: var(--bg2); border: 1px solid var(--border); border-radius: 999px;
  padding: 4px 12px 4px 7px; cursor: pointer; transition: border-color .15s, background .15s;
}
.cw-swatch:hover { border-color: var(--accent2); }
.cw-swatch.active { border-color: var(--accent); background: var(--bg3); }
.cw-dot {
  width: 16px; height: 16px; border-radius: 50%; flex: 0 0 auto;
  border: 1px solid rgba(255,255,255,.25); box-shadow: inset 0 0 0 1px rgba(0,0,0,.25);
}
.cw-pop {
  position: absolute; z-index: 60; top: 100%; margin-top: 8px; width: 232px; padding: 14px;
  background: var(--bg2); border: 1px solid var(--border); border-radius: 14px;
  box-shadow: 0 12px 34px rgba(0,0,0,.5);
}
.cw-pop[hidden] { display: none; }
.cw-pop-title {
  font-size: 12px; color: var(--fg2); margin-bottom: 12px;
  display: flex; justify-content: space-between; align-items: center;
}
.cw-pop-title .cw-live { color: var(--fg3); font-size: 10px; text-transform: uppercase; letter-spacing: .5px; }
.cw-wheel-wrap { position: relative; width: 200px; height: 200px; margin: 0 auto 12px; }
.cw-wheel { width: 200px; height: 200px; border-radius: 50%; display: block; cursor: crosshair; touch-action: none; }
.cw-thumb {
  position: absolute; width: 14px; height: 14px; border-radius: 50%; border: 2px solid #fff;
  box-shadow: 0 0 0 1px rgba(0,0,0,.55), 0 1px 3px rgba(0,0,0,.5);
  transform: translate(-50%, -50%); pointer-events: none;
}
.cw-bri {
  -webkit-appearance: none; appearance: none; width: 100%; height: 14px; border-radius: 7px;
  outline: none; cursor: pointer; margin: 0 0 12px; border: 1px solid var(--border);
}
.cw-bri::-webkit-slider-thumb {
  -webkit-appearance: none; width: 16px; height: 16px; border-radius: 50%; background: #fff;
  border: 2px solid rgba(0,0,0,.45); box-shadow: 0 1px 3px rgba(0,0,0,.5); cursor: pointer;
}
.cw-bri::-moz-range-thumb {
  width: 16px; height: 16px; border-radius: 50%; background: #fff;
  border: 2px solid rgba(0,0,0,.45); cursor: pointer;
}
.cw-row { display: flex; gap: 8px; align-items: center; margin-bottom: 11px; }
.cw-hex {
  flex: 1; font: 12px ui-monospace, Menlo, monospace; text-transform: uppercase;
  background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px 8px; outline: none;
}
.cw-hex:focus { border-color: var(--accent); }
.cw-preview { width: 32px; height: 32px; border-radius: 8px; border: 1px solid var(--border); flex: 0 0 auto; }
.cw-presets { display: flex; flex-wrap: wrap; gap: 6px; }
.cw-preset { width: 18px; height: 18px; border-radius: 5px; border: 1px solid rgba(255,255,255,.18); cursor: pointer; padding: 0; transition: transform .1s; }
.cw-preset:hover { transform: scale(1.15); }
#matrix-chart { min-height: 760px; }
.chart-hint {
  color: var(--fg3); font-size: 11px; margin-bottom: 8px;
  font-style: italic;
}
</style>
</head>
<body>

@@NAV:statistics@@

<div class="card">
  <div class="controls">
    <div class="group">
      <button class="btn preset-btn" data-preset="today">Heute</button>
      <button class="btn preset-btn" data-preset="7d">7 Tage</button>
      <button class="btn preset-btn" data-preset="30d">30 Tage</button>
    </div>
    <div class="group">
      <label>Von <input type="date" id="date-start"></label>
      <label>Bis <input type="date" id="date-end"></label>
      <button class="btn primary" id="refresh-btn">Aktualisieren</button>
    </div>
    <div class="group">
      <label><input type="checkbox" id="hide-empty" checked> Ohne Topic/Projekt ausblenden</label>
    </div>
    <div class="group">
      <label>Top (pro Ebene)
        <select id="top-n">
          <option value="5">5</option>
          <option value="10">10</option>
          <option value="25">25</option>
          <option value="50" selected>50</option>
          <option value="100">100</option>
          <option value="all">Alle</option>
        </select>
      </label>
    </div>
  </div>
  <div class="summary" id="summary"><div class="wt-loading inline"><div class="wt-spinner sm"></div><span class="wt-load-label">Lade Statistik…</span></div></div>
</div>

<div class="card">
  <div class="tab-switcher">
    <button class="tab-btn active" data-tab="crossfilter">Zusammenhänge</button>
    <button class="tab-btn" data-tab="categories">Kategorien</button>
    <button class="tab-btn" data-tab="sankey">Sankey</button>
    <button class="tab-btn" data-tab="matrix">Matrix</button>
  </div>

  <!-- Tab 1: Crossfilter -->
  <div class="tab-panel active" id="panel-crossfilter" data-tab="crossfilter">
    <div class="selection-bar">
      <span id="selection-info">Keine Auswahl — klicke Zeilen zum Filtern</span>
      <div class="spacer"></div>
      <button class="btn" id="reset-btn">Reset</button>
    </div>
    <div class="cf-cols">
      <div class="cf-col col-topic">
        <h3>Topics <span class="count">0</span></h3>
        <div class="cf-rows" id="col-topics"></div>
      </div>
      <div class="cf-col col-project">
        <h3>Projects <span class="count">0</span></h3>
        <div class="cf-rows" id="col-projects"></div>
      </div>
      <div class="cf-col col-app">
        <h3>Apps <span class="count">0</span></h3>
        <div class="cf-rows" id="col-apps"></div>
      </div>
    </div>
  </div>

  <!-- Tab: Kategorien (Kategorie-Pools der Projekte) -->
  <div class="tab-panel" id="panel-categories" data-tab="categories">
    <div class="selection-bar">
      <span id="cat-selection-info">Kategorie-Pools — klicke eine Kategorie, um alle ihre Projekte zu filtern</span>
      <div class="spacer"></div>
      <button class="btn" id="cat-reset-btn">Reset</button>
    </div>
    <div class="chart-hint">Zeit gruppiert nach Projekt-Kategorie. Jeder Pool listet seine Projekte. Filter aus dem Zusammenhänge-Tab gilt hier ebenfalls.</div>
    <div id="categories-list"></div>
  </div>

  <!-- Tab 2: Sankey -->
  <div class="tab-panel" id="panel-sankey" data-tab="sankey">
    <div class="color-controls">
      <span class="cc-label">Sankey-Farben:</span>
      <button type="button" class="cw-swatch" data-field="topic"><span class="cw-dot"></span>Topics</button>
      <button type="button" class="cw-swatch" data-field="project"><span class="cw-dot"></span>Projects</button>
      <button type="button" class="cw-swatch" data-field="app"><span class="cw-dot"></span>Apps</button>
      <button class="btn" id="color-reset">Farben zurücksetzen</button>
      <div class="cw-pop" id="cw-pop" hidden>
        <div class="cw-pop-title"><span id="cw-pop-label">Topics</span><span class="cw-live">live</span></div>
        <div class="cw-wheel-wrap">
          <canvas class="cw-wheel" id="cw-wheel" width="200" height="200"></canvas>
          <div class="cw-thumb" id="cw-thumb"></div>
        </div>
        <input type="range" class="cw-bri" id="cw-bri" min="0" max="100" value="100">
        <div class="cw-row">
          <input type="text" class="cw-hex" id="cw-hex" maxlength="7" spellcheck="false">
          <div class="cw-preview" id="cw-preview"></div>
        </div>
        <div class="cw-presets" id="cw-presets"></div>
      </div>
    </div>
    <div class="chart-hint">Fluss Topic → Project → App. Linkbreite = summierte Dauer. Zeigt <span class="top-n-label">Top 50</span> pro Ebene. Filter aus dem Zusammenhänge-Tab gilt hier ebenfalls.</div>
    <div class="chart-box" id="sankey-chart"></div>
  </div>

  <!-- Tab 3: Matrix -->
  <div class="tab-panel" id="panel-matrix" data-tab="matrix">
    <div class="matrix-controls">
      <label>Zeilen:
        <select id="matrix-row">
          <option value="topic" selected>Topic</option>
          <option value="project">Project</option>
          <option value="app">App</option>
        </select>
      </label>
      <label>Spalten:
        <select id="matrix-col">
          <option value="topic">Topic</option>
          <option value="project" selected>Project</option>
          <option value="app">App</option>
        </select>
      </label>
    </div>
    <div class="chart-hint">Farbintensität = summierte Dauer. Tooltip zeigt die Top 3 der jeweils dritten Dimension. Zeigt <span class="top-n-label">Top 50</span> pro Achse.</div>
    <div class="chart-box" id="matrix-chart"></div>
  </div>
</div>

<script>
const API = '/api/statistics';
// DEFAULT_COLORS muss VOR `state` stehen: der state-Initializer ruft
// loadColors() auf, das DEFAULT_COLORS liest. Stünde es danach, läge
// DEFAULT_COLORS in der Temporal Dead Zone und der ganze Script-Block
// bräche ab (→ Statistics-Seite hängt im „Lade…“-Spinner).
const DEFAULT_COLORS = { topic: '#58a6ff', project: '#bc8cff', app: '#3fb950' };

const state = {
  triples: [],
  meta: {},
  selection: { topic: new Set(), project: new Set(), app: new Set() },
  activeTab: 'crossfilter',
  hideEmpty: true,
  topN: 50,                       // Top-N per level (Topic/Project/App).
                                  // Infinity means "show all".
  matrixRow: 'topic',
  matrixCol: 'project',
  charts: { sankey: null, matrix: null },
  colors: loadColors(),           // Spaltenfarben (Topic/Project/App), persistiert.
};

function loadColors() {
  try {
    const saved = JSON.parse(localStorage.getItem('wt-stats-colors') || '{}');
    return Object.assign({}, DEFAULT_COLORS, saved);
  } catch (e) {
    return Object.assign({}, DEFAULT_COLORS);
  }
}

function applyColors() {
  const root = document.documentElement;
  root.style.setProperty('--col-topic', state.colors.topic);
  root.style.setProperty('--col-project', state.colors.project);
  root.style.setProperty('--col-app', state.colors.app);
  document.querySelectorAll('.cw-swatch').forEach(b => {
    const dot = b.querySelector('.cw-dot');
    if (dot) dot.style.background = state.colors[b.dataset.field];
  });
}

function saveColors() {
  localStorage.setItem('wt-stats-colors', JSON.stringify(state.colors));
}

// ---- Colorwheel (HSV, dependency-free) -----------------------------------
const CW = { field: null, h: 0, s: 0, v: 1, R: 98, cx: 100, cy: 100 };
const CW_PRESETS = ['#58a6ff','#bc8cff','#3fb950','#ff7b72','#f0883e','#e3b341',
                    '#39c5cf','#db61a2','#a371f7','#2ea043','#f85149','#8b949e'];

function hsvToRgb(h, s, v) {
  const c = v*s, x = c*(1-Math.abs((h/60)%2-1)), m = v-c;
  let r,g,b;
  if (h<60){r=c;g=x;b=0;} else if(h<120){r=x;g=c;b=0;}
  else if(h<180){r=0;g=c;b=x;} else if(h<240){r=0;g=x;b=c;}
  else if(h<300){r=x;g=0;b=c;} else {r=c;g=0;b=x;}
  return { r: Math.round((r+m)*255), g: Math.round((g+m)*255), b: Math.round((b+m)*255) };
}
function rgbToHsv(r, g, b) {
  r/=255; g/=255; b/=255;
  const mx=Math.max(r,g,b), mn=Math.min(r,g,b), d=mx-mn;
  let h=0;
  if (d) { if (mx===r) h=((g-b)/d)%6; else if (mx===g) h=(b-r)/d+2; else h=(r-g)/d+4; h*=60; if (h<0) h+=360; }
  return { h, s: mx ? d/mx : 0, v: mx };
}
function hexToRgb(hex) {
  hex = (hex||'').replace('#','').trim();
  if (hex.length===3) hex = hex.split('').map(c=>c+c).join('');
  if (!/^[0-9a-fA-F]{6}$/.test(hex)) return null;
  const n = parseInt(hex,16); return { r:(n>>16)&255, g:(n>>8)&255, b:n&255 };
}
function rgbToHex(r,g,b){ return '#'+[r,g,b].map(x=>x.toString(16).padStart(2,'0')).join(''); }
function hsvToHex(h,s,v){ const c=hsvToRgb(h,s,v); return rgbToHex(c.r,c.g,c.b); }

// Zeichnet das HSV-Rad (Winkel = Farbton, Radius = Sättigung) beim aktuellen
// Helligkeitswert CW.v auf das Canvas.
function cwDrawWheel() {
  const cv = $('cw-wheel'); if (!cv) return;
  const ctx = cv.getContext('2d');
  const img = ctx.createImageData(200,200), d = img.data, R = CW.R;
  for (let y=0;y<200;y++) for (let x=0;x<200;x++){
    const dx=x-CW.cx, dy=y-CW.cy, dist=Math.sqrt(dx*dx+dy*dy), i=(y*200+x)*4;
    if (dist<=R+1) {
      let h=Math.atan2(dy,dx)*180/Math.PI; if (h<0) h+=360;
      const c=hsvToRgb(h, Math.min(1,dist/R), CW.v);
      d[i]=c.r; d[i+1]=c.g; d[i+2]=c.b;
      d[i+3]= dist>R ? Math.max(0, Math.round(255*(1-(dist-R)))) : 255;
    } else d[i+3]=0;
  }
  ctx.putImageData(img,0,0);
}
function cwPositionThumb() {
  const ang=CW.h*Math.PI/180, r=CW.s*CW.R, th=$('cw-thumb');
  th.style.left=(CW.cx + r*Math.cos(ang))+'px';
  th.style.top =(CW.cy + r*Math.sin(ang))+'px';
  th.style.background=hsvToHex(CW.h, CW.s, CW.v);
}
// Spiegelt CW → UI; mit writeState=true zusätzlich nach state.colors +
// Sankey/Crossfilter live.
function cwSync(writeState) {
  const hex = hsvToHex(CW.h, CW.s, CW.v);
  cwPositionThumb();
  $('cw-preview').style.background = hex;
  if (document.activeElement !== $('cw-hex')) $('cw-hex').value = hex.toUpperCase();
  $('cw-bri').style.background = 'linear-gradient(90deg,#000,'+hsvToHex(CW.h, CW.s, 1)+')';
  if (writeState && CW.field) {
    state.colors[CW.field] = hex;
    saveColors(); applyColors();
    if (state.activeTab === 'sankey') renderSankey();
  }
}
function cwPick(ev) {
  const cv=$('cw-wheel'), rect=cv.getBoundingClientRect();
  const x=(ev.clientX-rect.left)*(200/rect.width), y=(ev.clientY-rect.top)*(200/rect.height);
  let dx=x-CW.cx, dy=y-CW.cy, dist=Math.sqrt(dx*dx+dy*dy);
  if (dist>CW.R){ const k=CW.R/dist; dx*=k; dy*=k; dist=CW.R; }
  let h=Math.atan2(dy,dx)*180/Math.PI; if (h<0) h+=360;
  CW.h=h; CW.s=Math.min(1, dist/CW.R);
  cwSync(true);
}
function cwOpen(field, btn) {
  CW.field = field;
  const rgb = hexToRgb(state.colors[field]) || {r:88,g:166,b:255};
  const hsv = rgbToHsv(rgb.r, rgb.g, rgb.b);
  CW.h=hsv.h; CW.s=hsv.s; CW.v=hsv.v;
  $('cw-pop-label').textContent = btn.textContent.trim();
  $('cw-bri').value = Math.round(CW.v*100);
  const pop = $('cw-pop');
  pop.style.left = Math.max(0, Math.min(btn.offsetLeft, btn.parentElement.clientWidth - 232)) + 'px';
  pop.hidden = false;
  document.querySelectorAll('.cw-swatch').forEach(b => b.classList.toggle('active', b===btn));
  cwDrawWheel(); cwSync(false);
}
function cwClose() {
  $('cw-pop').hidden = true;
  document.querySelectorAll('.cw-swatch').forEach(b => b.classList.remove('active'));
  CW.field = null;
}
function cwSetHsvFromHex(hex) {
  const rgb = hexToRgb(hex); if (!rgb) return false;
  const hsv = rgbToHsv(rgb.r, rgb.g, rgb.b);
  CW.h=hsv.h; CW.s=hsv.s; CW.v=hsv.v;
  $('cw-bri').value = Math.round(CW.v*100);
  cwDrawWheel(); cwSync(true);
  return true;
}

// Helper: apply state.topN to a sorted list. Infinity → return as-is.
function applyTopN(items) {
  return Number.isFinite(state.topN) ? items.slice(0, state.topN) : items;
}

// Helper: format the current Top-N for hint labels.
function topNLabel() {
  return Number.isFinite(state.topN) ? ('Top ' + state.topN) : 'Alle';
}

const $ = id => document.getElementById(id);

// Aktuelle CSS-Variable auslesen — damit die ECharts dem Light/Dark-Theme folgen.
function themeCol(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function fmtTime(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h > 0) return h + 'h ' + m + 'm';
  if (m > 0) return m + 'm';
  return sec + 's';
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function todayStr() {
  const d = new Date();
  return d.getFullYear() + '-' +
    String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0');
}

function daysAgoStr(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.getFullYear() + '-' +
    String(d.getMonth() + 1).padStart(2, '0') + '-' +
    String(d.getDate()).padStart(2, '0');
}

function setPreset(name) {
  document.querySelectorAll('.preset-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.preset === name);
  });
  const end = todayStr();
  let start = end;
  if (name === '7d') start = daysAgoStr(6);
  else if (name === '30d') start = daysAgoStr(29);
  $('date-start').value = start;
  $('date-end').value = end;
  loadStats();
}

async function loadStats() {
  const start = $('date-start').value;
  const end = $('date-end').value;
  if (!start || !end) return;
  $('summary').innerHTML = '<div class="wt-loading inline"><div class="wt-spinner sm"></div><span class="wt-load-label">Lade Statistik…</span></div>';
  try {
    const r = await fetch(API + '?start=' + start + '&end=' + end);
    const data = await r.json();
    if (data.error) {
      $('summary').textContent = 'Fehler: ' + data.error;
      return;
    }
    state.triples = data.triples || [];
    state.meta = data;
    state.selection = { topic: new Set(), project: new Set(), app: new Set() };
    renderAll();
  } catch (e) {
    $('summary').textContent = 'Fehler beim Laden: ' + e.message;
  }
}

function filteredTriples(exceptField) {
  const sel = state.selection;
  return state.triples.filter(t => {
    if (state.hideEmpty && (t.topic === '(ohne Topic)' || t.project === '(ohne Projekt)')) return false;
    if (exceptField !== 'topic' && sel.topic.size && !sel.topic.has(t.topic)) return false;
    if (exceptField !== 'project' && sel.project.size && !sel.project.has(t.project)) return false;
    if (exceptField !== 'app' && sel.app.size && !sel.app.has(t.app)) return false;
    return true;
  });
}

function aggregateBy(field, triples) {
  const buckets = new Map();
  for (const t of triples) {
    const k = t[field];
    if (!buckets.has(k)) buckets.set(k, { name: k, sec: 0, count: 0 });
    const b = buckets.get(k);
    b.sec += t.sec;
    b.count += t.count;
  }
  return [...buckets.values()].sort((a, b) => b.sec - a.sec);
}

function updateSummary() {
  const m = state.meta;
  const filtered = filteredTriples();
  const secSum = filtered.reduce((a, t) => a + t.sec, 0);
  const cntSum = filtered.reduce((a, t) => a + t.count, 0);
  const combos = filtered.length;
  const total = state.triples.length;
  const rangeStr = (m.start === m.end) ? (m.start || '') : ((m.start || '') + ' → ' + (m.end || ''));
  const comboTxt = (combos === total) ? combos : (combos + ' / ' + total);
  $('summary').innerHTML =
    'Zeitraum: <strong>' + escapeHtml(rangeStr) + '</strong>' +
    ' · Tage mit Daten: <strong>' + (m.days_with_data || 0) + '</strong>' +
    ' · Gesamtzeit: <strong>' + fmtTime(secSum) + '</strong>' +
    ' · Sessions: <strong>' + cntSum + '</strong>' +
    ' · Kombinationen: <strong>' + comboTxt + '</strong>';
}

function renderCrossfilter() {
  const sel = state.selection;
  const selTexts = [];
  if (sel.topic.size) selTexts.push(sel.topic.size + ' Topic(s)');
  if (sel.project.size) selTexts.push(sel.project.size + ' Projekt(e)');
  if (sel.app.size) selTexts.push(sel.app.size + ' App(s)');
  $('selection-info').textContent = selTexts.length
    ? 'Auswahl: ' + selTexts.join(' · ') + ' (AND zwischen Spalten, OR innerhalb)'
    : 'Keine Auswahl — klicke Zeilen zum Filtern';

  const cols = [
    { field: 'topic', el: 'col-topics' },
    { field: 'project', el: 'col-projects' },
    { field: 'app', el: 'col-apps' },
  ];
  cols.forEach(c => {
    // Each column sees triples filtered by the OTHER columns only,
    // so selecting in a column doesn't collapse that column to 1 row.
    const allItems = aggregateBy(c.field, filteredTriples(c.field));
    const items = applyTopN(allItems);
    const max = (items[0] && items[0].sec) || 1;
    const container = $(c.el);
    const countEl = container.parentElement.querySelector('.count');
    // Show "visible / total" when Top-N clips the list.
    if (countEl) {
      countEl.textContent = (items.length < allItems.length)
        ? (items.length + ' / ' + allItems.length)
        : items.length;
    }
    if (!items.length) {
      container.innerHTML = '<div class="cf-empty">Keine Daten</div>';
      return;
    }
    const selectedSet = sel[c.field];
    container.innerHTML = items.map(it => {
      const selected = selectedSet.has(it.name);
      const pct = Math.round((it.sec / max) * 100);
      return '<div class="cf-row' + (selected ? ' selected' : '') +
        '" data-field="' + c.field + '" data-name="' + escapeHtml(it.name) + '">' +
        '<span class="cf-name" title="' + escapeHtml(it.name) + '">' + escapeHtml(it.name) + '</span>' +
        '<span class="cf-bar-wrap"><span class="cf-bar" style="width:' + pct + '%"></span></span>' +
        '<span class="cf-time">' + fmtTime(it.sec) + '</span>' +
        '<span class="cf-count">' + it.count + '</span>' +
        '</div>';
    }).join('');
  });
}

function toggleSelect(field, name) {
  const set = state.selection[field];
  if (set.has(name)) set.delete(name); else set.add(name);
  renderAll();
}

// Kategorie-Farben aus dem zentralen Pool (/api/categories); die Konstanten
// hier sind nur Fallback, bis der Pool geladen ist.
let CAT_COLORS = {
  "Development": "#6fe28a", "Business": "#ffb347", "Creative": "#ff79c6",
  "Music": "#c792ea", "Crypto": "#f5d400", "Finance": "#4fc3d8",
  "AI": "#d4f500", "News": "#ff8a5c", "Communication": "#7aa2f7",
  "Social Media": "#5ab0f7", "Entertainment": "#ff5a8a", "Research": "#9ece6a",
};
let CAT_ALIAS = {};
function canonCat(cat) {
  if (!cat) return cat;
  return CAT_ALIAS[String(cat).toLowerCase()] || cat;
}
async function loadCatPool() {
  try {
    const d = await (await fetch('/api/categories')).json();
    const m = {};
    for (const [n, i] of Object.entries(d.tool || {})) if (i && i.color) m[n] = i.color;
    for (const [n, i] of Object.entries(d.activity || {})) if (i && i.color) m[n] = i.color;
    CAT_COLORS = m;
    CAT_ALIAS = d.alias_map || {};
  } catch (e) { /* Fallback-Farben bleiben */ }
}
function catColor(cat) {
  cat = canonCat(cat);
  if (!cat || cat === 'Ohne Kategorie') return '#8a95a3';
  if (CAT_COLORS[cat]) return CAT_COLORS[cat];
  let h = 0;
  for (const ch of cat) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return 'hsl(' + h + ', 65%, 62%)';
}

const CAT_NONE = 'Ohne Kategorie';

function renderCategories() {
  const container = $('categories-list');
  // 'project' ausnehmen, damit Projekt-Auswahlen die Pool-Liste nicht kollabieren.
  const triples = filteredTriples('project');

  const sel = state.selection.project;
  $('cat-selection-info').textContent = sel.size
    ? 'Auswahl: ' + sel.size + ' Projekt(e) — wirkt auch in den anderen Tabs'
    : 'Kategorie-Pools — klicke eine Kategorie, um alle ihre Projekte zu filtern';

  // Gruppieren: Kategorie → {sec,count, projs: Map}
  const cats = new Map();
  for (const t of triples) {
    const cat = (t.category && t.category.trim()) ? t.category.trim() : CAT_NONE;
    if (!cats.has(cat)) cats.set(cat, { name: cat, sec: 0, count: 0, projs: new Map() });
    const c = cats.get(cat);
    c.sec += t.sec; c.count += t.count;
    if (!c.projs.has(t.project)) c.projs.set(t.project, { name: t.project, sec: 0, count: 0 });
    const p = c.projs.get(t.project);
    p.sec += t.sec; p.count += t.count;
  }

  let pools = [...cats.values()].sort((a, b) => b.sec - a.sec);
  pools = applyTopN(pools);
  if (!pools.length) {
    container.innerHTML = '<div class="cf-empty">Keine Daten im Zeitraum</div>';
    return;
  }
  const max = pools[0].sec || 1;

  container.innerHTML = '<div class="cat-pools">' + pools.map(c => {
    const col = catColor(c.name);
    const projs = [...c.projs.values()].sort((a, b) => b.sec - a.sec);
    const pmax = projs[0] ? projs[0].sec : 1;
    // ausgewählt, wenn ALLE Projekte des Pools selektiert sind
    const allSel = projs.length && projs.every(p => sel.has(p.name));
    const pct = Math.round((c.sec / max) * 100);
    const rows = projs.map(p => {
      const psel = sel.has(p.name);
      const pp = Math.round((p.sec / pmax) * 100);
      return '<div class="cat-prow' + (psel ? ' selected' : '') +
        '" data-name="' + escapeHtml(p.name) + '">' +
        '<span class="cf-name" title="' + escapeHtml(p.name) + '">' + escapeHtml(p.name) + '</span>' +
        '<span class="cf-bar-wrap"><span class="cf-bar" style="width:' + pp + '%;background:' + col + '"></span></span>' +
        '<span class="cf-time">' + fmtTime(p.sec) + '</span>' +
        '<span class="cf-count">' + p.count + '</span>' +
        '</div>';
    }).join('');
    return '<div class="cat-pool' + (allSel ? ' selected' : '') + '" style="border-left-color:' + col + '">' +
      '<div class="cat-pool-head" data-cat="' + escapeHtml(c.name) + '">' +
        '<span class="cat-pool-dot" style="background:' + col + '"></span>' +
        '<span class="cat-pool-name">' + escapeHtml(c.name) + '</span>' +
        '<span class="cat-pool-time">' + fmtTime(c.sec) + '</span>' +
        '<span class="cat-pool-meta">' + projs.length + ' Proj · ' + c.count + ' Sess</span>' +
      '</div>' +
      '<div class="cat-pool-projs">' + rows + '</div>' +
    '</div>';
  }).join('') + '</div>';
}

// Klick auf eine Kategorie selektiert/deselektiert alle ihre Projekte.
function toggleCategory(catName) {
  const triples = filteredTriples('project');
  const projs = new Set();
  for (const t of triples) {
    const cat = (t.category && t.category.trim()) ? t.category.trim() : CAT_NONE;
    if (cat === catName) projs.add(t.project);
  }
  const set = state.selection.project;
  const allSel = [...projs].every(p => set.has(p));
  if (allSel) { for (const p of projs) set.delete(p); }
  else { for (const p of projs) set.add(p); }
  renderAll();
}

function resetSelection() {
  state.selection = { topic: new Set(), project: new Set(), app: new Set() };
  renderAll();
}

function showEmptyChart(chart, msg) {
  chart.clear();
  chart.setOption({
    backgroundColor: 'transparent',
    title: {
      text: msg, left: 'center', top: 'center',
      textStyle: { color: themeCol('--fg2', '#8b949e'), fontSize: 14, fontWeight: 'normal' },
    },
  });
}

function renderSankey() {
  if (typeof echarts === 'undefined') {
    $('sankey-chart').innerHTML = '<div class="cf-empty" style="padding:24px">Diagramm-Bibliothek (echarts) konnte nicht geladen werden — bitte Internetverbindung prüfen.</div>';
    return;
  }
  if (!state.charts.sankey) {
    state.charts.sankey = echarts.init($('sankey-chart'), null, { renderer: 'canvas' });
  }
  const chart = state.charts.sankey;
  const triples = filteredTriples();
  if (!triples.length) {
    showEmptyChart(chart, 'Keine Daten im Zeitraum');
    return;
  }
  // Limit each axis to top N by total duration, otherwise labels overlap.
  // state.topN controls this; Infinity means "show every distinct value".
  const topTotals = new Map(), projTotals = new Map(), appTotals = new Map();
  for (const t of triples) {
    topTotals.set(t.topic, (topTotals.get(t.topic) || 0) + t.sec);
    projTotals.set(t.project, (projTotals.get(t.project) || 0) + t.sec);
    appTotals.set(t.app, (appTotals.get(t.app) || 0) + t.sec);
  }
  const topKeep  = new Set(applyTopN([...topTotals.entries() ].sort((a,b)=>b[1]-a[1])).map(x=>x[0]));
  const projKeep = new Set(applyTopN([...projTotals.entries()].sort((a,b)=>b[1]-a[1])).map(x=>x[0]));
  const appKeep  = new Set(applyTopN([...appTotals.entries() ].sort((a,b)=>b[1]-a[1])).map(x=>x[0]));
  const keptTriples = triples.filter(t => topKeep.has(t.topic) && projKeep.has(t.project) && appKeep.has(t.app));
  if (!keptTriples.length) {
    showEmptyChart(chart, 'Keine überschneidenden Top-Einträge');
    return;
  }
  const nodes = new Map();
  const tp = new Map();
  const pa = new Map();
  for (const t of keptTriples) {
    const tN = 'T: ' + t.topic;
    const pN = 'P: ' + t.project;
    const aN = 'A: ' + t.app;
    if (!nodes.has(tN)) nodes.set(tN, { name: tN, itemStyle: { color: state.colors.topic }, depth: 0 });
    if (!nodes.has(pN)) nodes.set(pN, { name: pN, itemStyle: { color: state.colors.project }, depth: 1 });
    if (!nodes.has(aN)) nodes.set(aN, { name: aN, itemStyle: { color: state.colors.app }, depth: 2 });
    const k1 = tN + '\u0001' + pN;
    tp.set(k1, (tp.get(k1) || 0) + t.sec);
    const k2 = pN + '\u0001' + aN;
    pa.set(k2, (pa.get(k2) || 0) + t.sec);
  }
  const links = [];
  for (const [k, v] of tp) {
    const [s, d] = k.split('\u0001');
    links.push({ source: s, target: d, value: v });
  }
  for (const [k, v] of pa) {
    const [s, d] = k.split('\u0001');
    links.push({ source: s, target: d, value: v });
  }
  // Höhe an die größte Spalte anpassen, damit alle Knoten + Labels Platz haben:
  // mind. 18px pro Knoten (Node + Gap) plus Rand, nie unter 600px.
  const depthCounts = [0, 0, 0];
  for (const n of nodes.values()) depthCounts[n.depth]++;
  const maxNodes = Math.max(...depthCounts);
  const neededPx = Math.max(600, maxNodes * 18 + 40);
  const box = $('sankey-chart');
  if (Math.abs(box.clientHeight - neededPx) > 4) {
    box.style.height = neededPx + 'px';
    chart.resize();
  }

  chart.clear();
  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: themeCol('--bg2', '#161b22'),
      borderColor: themeCol('--border', '#21262d'),
      textStyle: { color: themeCol('--fg', '#e6edf3') },
      formatter: (p) => {
        if (p.dataType === 'edge') {
          return escapeHtml(p.data.source) + '<br>→ ' + escapeHtml(p.data.target) +
            '<br><b>' + fmtTime(p.data.value) + '</b>';
        }
        return escapeHtml(p.data.name) + '<br><b>' + fmtTime(p.value || 0) + '</b>';
      },
    },
    series: [{
      type: 'sankey',
      data: [...nodes.values()],
      links: links,
      emphasis: { focus: 'adjacency' },
      lineStyle: { color: 'gradient', curveness: 0.5, opacity: 0.45 },
      label: { color: themeCol('--fg', '#e6edf3'), fontSize: 11 },
      nodeGap: 8,
      nodeWidth: 14,
      left: 10, right: 100, top: 10, bottom: 10,
    }],
  });
}

function renderMatrix() {
  if (typeof echarts === 'undefined') {
    $('matrix-chart').innerHTML = '<div class="cf-empty" style="padding:24px">Diagramm-Bibliothek (echarts) konnte nicht geladen werden — bitte Internetverbindung prüfen.</div>';
    return;
  }
  if (!state.charts.matrix) {
    state.charts.matrix = echarts.init($('matrix-chart'), null, { renderer: 'canvas' });
  }
  const chart = state.charts.matrix;
  const triples = filteredTriples();
  if (!triples.length) {
    showEmptyChart(chart, 'Keine Daten im Zeitraum');
    return;
  }
  const row = state.matrixRow;
  const col = state.matrixCol;
  const third = ['topic', 'project', 'app'].find(d => d !== row && d !== col);

  // Top-N per axis by total duration (configurable via state.topN).
  const rowTotals = new Map();
  const colTotals = new Map();
  for (const t of triples) {
    rowTotals.set(t[row], (rowTotals.get(t[row]) || 0) + t.sec);
    colTotals.set(t[col], (colTotals.get(t[col]) || 0) + t.sec);
  }
  const topRows = applyTopN([...rowTotals.entries()].sort((a, b) => b[1] - a[1])).map(x => x[0]);
  const topCols = applyTopN([...colTotals.entries()].sort((a, b) => b[1] - a[1])).map(x => x[0]);
  const rowKeep = new Set(topRows);
  const colKeep = new Set(topCols);
  const rowIdx = new Map(topRows.map((r, i) => [r, i]));
  const colIdx = new Map(topCols.map((c, i) => [c, i]));

  // Aggregate
  const cellAgg = new Map();
  for (const t of triples) {
    const rv = t[row], cv = t[col];
    if (!rowKeep.has(rv) || !colKeep.has(cv)) continue;
    const key = rv + '\u0001' + cv;
    if (!cellAgg.has(key)) cellAgg.set(key, { sec: 0, count: 0, third: new Map() });
    const b = cellAgg.get(key);
    b.sec += t.sec;
    b.count += t.count;
    if (third) b.third.set(t[third], (b.third.get(t[third]) || 0) + t.sec);
  }

  const data = [];
  let maxVal = 0;
  for (const [key, b] of cellAgg) {
    const [rv, cv] = key.split('\u0001');
    data.push([colIdx.get(cv), rowIdx.get(rv), b.sec, b.count, rv, cv]);
    if (b.sec > maxVal) maxVal = b.sec;
  }
  if (!maxVal) maxVal = 1;
  // Robust color scale: use 90th percentile as visualMap max so a few
  // outlier cells don't flatten the rest of the heatmap into darkness.
  const sortedSecs = data.map(d => d[2]).sort((a, b) => a - b);
  let visualMax = maxVal;
  if (sortedSecs.length >= 10) {
    visualMax = sortedSecs[Math.floor(sortedSecs.length * 0.9)] || maxVal;
    if (visualMax < maxVal / 20) visualMax = maxVal / 20;
  }

  chart.clear();
  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      backgroundColor: themeCol('--bg2', '#161b22'),
      borderColor: themeCol('--border', '#21262d'),
      textStyle: { color: themeCol('--fg', '#e6edf3') },
      formatter: (p) => {
        const rv = p.data[4], cv = p.data[5];
        const sec = p.data[2], cnt = p.data[3];
        const key = rv + '\u0001' + cv;
        const b = cellAgg.get(key);
        let html = '<b>' + escapeHtml(rv) + '</b> × <b>' + escapeHtml(cv) + '</b><br>' +
          fmtTime(sec) + ' · ' + cnt + ' Sessions';
        if (third && b && b.third.size) {
          const topThird = [...b.third.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3);
          html += '<br><span style="color:' + themeCol('--fg2', '#8b949e') + '">Top ' + third + ':</span>';
          topThird.forEach(entry => {
            html += '<br>· ' + escapeHtml(entry[0]) + ' (' + fmtTime(entry[1]) + ')';
          });
        }
        return html;
      },
    },
    grid: { left: 160, right: 40, bottom: 140, top: 20 },
    xAxis: {
      type: 'category', data: topCols, splitArea: { show: true },
      axisLabel: { color: themeCol('--fg2', '#8b949e'), rotate: 45, interval: 0, fontSize: 10 },
      axisLine: { lineStyle: { color: themeCol('--fg3', '#484f58') } },
    },
    yAxis: {
      type: 'category', data: topRows, splitArea: { show: true },
      axisLabel: { color: themeCol('--fg2', '#8b949e'), fontSize: 10 },
      axisLine: { lineStyle: { color: themeCol('--fg3', '#484f58') } },
    },
    visualMap: {
      min: 0, max: visualMax, calculable: true,
      dimension: 2,
      orient: 'horizontal', left: 'center', bottom: 10,
      inRange: { color: document.documentElement.getAttribute('data-theme') === 'light'
        ? ['#e3e9ee', '#bfe5c8', '#3fb950', '#d29922', '#f85149']
        : ['#161b22', '#0d3321', '#3fb950', '#d29922', '#f85149'] },
      textStyle: { color: themeCol('--fg2', '#8b949e') },
      formatter: v => fmtTime(v),
    },
    series: [{
      name: 'Dauer', type: 'heatmap', data: data,
      encode: { x: 0, y: 1, value: 2 },
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: 'rgba(88, 166, 255, 0.5)' } },
    }],
  });
}

function switchTab(name) {
  state.activeTab = name;
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.classList.toggle('active', p.dataset.tab === name);
  });
  setTimeout(() => {
    if (name === 'sankey') {
      if (state.charts.sankey) state.charts.sankey.resize();
      renderSankey();
    } else if (name === 'matrix') {
      if (state.charts.matrix) state.charts.matrix.resize();
      renderMatrix();
    }
  }, 30);
}

function renderAll() {
  updateSummary();
  renderCrossfilter();
  renderCategories();
  if (state.activeTab === 'sankey') renderSankey();
  if (state.activeTab === 'matrix') renderMatrix();
}

function tickClock() {
  const d = new Date();
  $('clock').textContent = d.toLocaleTimeString('de-DE');
}

function init() {
  document.querySelectorAll('.preset-btn').forEach(b => {
    b.addEventListener('click', () => setPreset(b.dataset.preset));
  });
  $('refresh-btn').addEventListener('click', loadStats);
  $('hide-empty').addEventListener('change', e => {
    state.hideEmpty = e.target.checked;
    renderAll();
  });
  $('top-n').addEventListener('change', e => {
    const v = e.target.value;
    state.topN = (v === 'all') ? Infinity : parseInt(v, 10);
    document.querySelectorAll('.top-n-label').forEach(el => el.textContent = topNLabel());
    renderAll();
  });
  $('reset-btn').addEventListener('click', resetSelection);

  // Moderne Colorwheels (Sankey + Crossfilter-Balken), live + persistent.
  const cwPresetWrap = $('cw-presets');
  CW_PRESETS.forEach(hex => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'cw-preset';
    b.style.background = hex; b.title = hex; b.dataset.hex = hex;
    cwPresetWrap.appendChild(b);
  });
  document.querySelectorAll('.cw-swatch').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (CW.field === btn.dataset.field) { cwClose(); return; }
      cwOpen(btn.dataset.field, btn);
    });
  });
  const wheel = $('cw-wheel');
  let cwDragging = false;
  wheel.addEventListener('pointerdown', (e) => { cwDragging = true; wheel.setPointerCapture(e.pointerId); cwPick(e); });
  wheel.addEventListener('pointermove', (e) => { if (cwDragging) cwPick(e); });
  wheel.addEventListener('pointerup',   () => { cwDragging = false; });
  $('cw-bri').addEventListener('input', (e) => { CW.v = (+e.target.value)/100; cwDrawWheel(); cwSync(true); });
  $('cw-hex').addEventListener('input', (e) => { cwSetHsvFromHex(e.target.value); });
  cwPresetWrap.addEventListener('click', (e) => {
    const p = e.target.closest('.cw-preset'); if (!p) return;
    cwSetHsvFromHex(p.dataset.hex);
  });
  // Klick außerhalb von Popover/Swatch schließt das Wheel
  document.addEventListener('click', (e) => {
    if (!CW.field) return;
    if (e.target.closest('.cw-pop') || e.target.closest('.cw-swatch')) return;
    cwClose();
  });
  $('color-reset').addEventListener('click', () => {
    state.colors = Object.assign({}, DEFAULT_COLORS);
    saveColors();
    applyColors();
    if (CW.field) cwOpen(CW.field, document.querySelector('.cw-swatch[data-field="' + CW.field + '"]'));
    if (state.activeTab === 'sankey') renderSankey();
  });
  applyColors();

  document.querySelectorAll('.tab-btn').forEach(b => {
    b.addEventListener('click', () => switchTab(b.dataset.tab));
  });

  $('panel-crossfilter').addEventListener('click', e => {
    const row = e.target.closest('.cf-row');
    if (!row) return;
    toggleSelect(row.dataset.field, row.dataset.name);
  });

  $('panel-categories').addEventListener('click', e => {
    const prow = e.target.closest('.cat-prow');
    if (prow) { toggleSelect('project', prow.dataset.name); return; }
    const head = e.target.closest('.cat-pool-head');
    if (head) toggleCategory(head.dataset.cat);
  });
  $('cat-reset-btn').addEventListener('click', resetSelection);

  $('matrix-row').addEventListener('change', e => {
    state.matrixRow = e.target.value;
    renderMatrix();
  });
  $('matrix-col').addEventListener('change', e => {
    state.matrixCol = e.target.value;
    renderMatrix();
  });

  window.addEventListener('resize', () => {
    if (state.charts.sankey) state.charts.sankey.resize();
    if (state.charts.matrix) state.charts.matrix.resize();
  });

  // Light/Dark-Wechsel: Charts mit den neuen Theme-Farben neu aufbauen
  window.addEventListener('wt-theme', () => {
    if (state.activeTab === 'sankey') renderSankey();
    if (state.activeTab === 'matrix') renderMatrix();
  });

  tickClock();
  setInterval(tickClock, 1000);

  // Pool-Farben/-Aliase zuerst laden, damit der erste Render sie nutzt
  loadCatPool().finally(() => setPreset('7d'));
}

init();
</script>
</body>
</html>"""


SCREENSHOTS_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkTracker Screenshots</title>
<style>
:root {
  /* Palette mirrors docs/index.html — acid-green accent, slate-blue cyan */
  --bg: #0c1116; --bg2: #141a20; --bg3: #1b222a;
  --fg: #cfd7e0; --fg2: #8a95a3; --fg3: #484f58;
  --cyan: #4fc3d8; --green: #6fe28a; --yellow: #d29922; --acid: #d4f500;
  --red: #ff4d4f; --purple: #b392ff; --blue: #4fc3d8;
  --border: #2b3642; --white: #f4f8ff;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  background: var(--bg); color: var(--fg);
  font-size: 13px; line-height: 1.5;
  padding: 16px clamp(16px, 2.5vw, 36px) 48px; max-width: 1760px; margin: 0 auto;
}
h1 { font-size: 18px; color: var(--cyan); font-weight: 600; }
h1 a { color: var(--fg2); font-size: 12px; text-decoration: none; font-weight: 400; }
h1 a:hover { color: var(--cyan); }
.header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 0 12px; border-bottom: 1px solid var(--bg3); margin-bottom: 16px;
  flex-wrap: wrap; gap: 12px;
}
.toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.toolbar label { color: var(--fg2); font-size: 12px; }
.toolbar select, .toolbar input {
  background: var(--bg2); color: var(--fg); border: 1px solid var(--bg3);
  border-radius: 4px; padding: 4px 8px; font-family: inherit; font-size: 12px;
}
.toolbar select:focus, .toolbar input:focus {
  outline: none; border-color: var(--cyan);
}
.summary { color: var(--fg2); font-size: 12px; }
.empty { color: var(--fg3); padding: 40px 0; text-align: center; }

.grid {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
}
.card {
  background: var(--bg2); border: 1px solid var(--border);
  border-radius: 14px; overflow: hidden;
  display: flex; flex-direction: column;
  box-shadow: 0 1px 2px rgba(0,0,0,.25);
  transition: border-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;
}
.card:hover {
  border-color: var(--cyan); transform: translateY(-2px);
  box-shadow: 0 12px 28px -14px rgba(0,0,0,.55);
}
.card:hover .thumb { transform: scale(1.03); }
.thumb {
  width: 100%; aspect-ratio: 16 / 9; background: #000;
  display: block; cursor: zoom-in; object-fit: cover;
  transition: transform 0.3s ease;
}
.meta { padding: 10px 12px 12px; }
.meta-row1 {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: 8px; margin-bottom: 4px;
}
.time { color: var(--cyan); font-size: 12px; font-weight: 600; }
.app { color: var(--fg2); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 50%;
}
.topic {
  color: var(--fg); font-size: 12px; margin-bottom: 6px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.motivation {
  color: var(--purple); font-size: 12px; font-style: italic;
  border-top: 1px solid var(--bg3); padding-top: 6px; margin-top: 4px;
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical;
  overflow: hidden;
}
.motivation.empty-msg { color: var(--fg3); font-style: normal; }
.no-session { color: var(--fg3); font-size: 11px; padding-top: 6px; border-top: 1px solid var(--bg3); margin-top: 4px; }

/* Lightbox */
.lightbox {
  position: fixed; inset: 0; background: rgba(0,0,0,0.92);
  display: none; align-items: center; justify-content: center;
  padding: 20px; z-index: 100; cursor: zoom-out;
}
.lightbox.active { display: flex; }
.lightbox img {
  max-width: 100%; max-height: 100%; object-fit: contain;
  border-radius: 4px; box-shadow: 0 0 40px rgba(0,0,0,0.6);
}
.lightbox-info {
  position: absolute; bottom: 20px; left: 20px; right: 20px;
  background: rgba(13,17,23,0.85); padding: 10px 16px; border-radius: 6px;
  color: var(--fg); font-size: 12px; max-width: 800px; margin: 0 auto;
  pointer-events: none;
}
.lightbox-info .lb-time { color: var(--cyan); font-weight: 600; }
.lightbox-info .lb-app { color: var(--fg2); margin-left: 10px; }
.lightbox-info .lb-topic { color: var(--fg); margin-top: 4px; }
.lightbox-info .lb-mot { color: var(--purple); font-style: italic; margin-top: 4px; }

/* Datepicker */
.dp { position: relative; }
.dp-trigger {
  display: flex; align-items: center; gap: 7px;
  background: var(--bg2); color: var(--fg); border: 1px solid var(--bg3);
  border-radius: 6px; padding: 5px 10px; font-family: inherit; font-size: 12px;
  cursor: pointer; transition: border-color 0.15s;
}
.dp-trigger:hover { border-color: var(--cyan); }
.dp-trigger svg { color: var(--cyan); }
.dp-pop {
  position: absolute; top: calc(100% + 6px); right: 0; z-index: 60;
  background: var(--bg2); border: 1px solid var(--bg3); border-radius: 8px;
  padding: 10px; width: 232px; box-shadow: 0 8px 24px rgba(0,0,0,0.5);
}
.dp-pop[hidden] { display: none; }
.dp-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.dp-title { font-size: 12px; font-weight: 600; color: var(--fg); }
.dp-nav {
  background: none; border: none; color: var(--fg2); cursor: pointer;
  font-size: 18px; line-height: 1; padding: 2px 8px; border-radius: 5px;
}
.dp-nav:hover:not(:disabled) { background: var(--bg3); color: var(--fg); }
.dp-nav:disabled { opacity: 0.25; cursor: default; }
.dp-weekdays, .dp-grid {
  display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px;
}
.dp-weekdays span {
  text-align: center; font-size: 9px; color: var(--fg3);
  text-transform: uppercase; padding: 2px 0;
}
.dp-day {
  text-align: center; font-size: 11px; padding: 5px 0; border-radius: 5px;
  color: var(--fg3); user-select: none;
}
.dp-day.has {
  color: var(--fg); cursor: pointer; background: var(--bg3);
  position: relative;
}
.dp-day.has:hover { background: var(--cyan); color: var(--bg); }
.dp-day.sel { background: var(--cyan); color: var(--bg); font-weight: 700; }
.dp-day.dp-empty { visibility: hidden; padding: 0; }
.dp-day.today:not(.sel) { box-shadow: inset 0 0 0 1px var(--cyan); }
.dp-day.muted { color: var(--bg3); }
</style>
</head>
<body>
@@NAV:screenshots@@

<div id="grid" class="grid"></div>
<div id="empty" class="empty" style="display:none">Keine Screenshots fuer dieses Datum.</div>

<div id="lightbox" class="lightbox">
  <img id="lightbox-img" alt="">
  <div class="lightbox-info" id="lightbox-info"></div>
</div>

<script>
const $ = sel => document.querySelector(sel);
const grid = $('#grid');
const empty = $('#empty');
const summary = $('#summary');
const lightbox = $('#lightbox');
const lightboxImg = $('#lightbox-img');
const lightboxInfo = $('#lightbox-info');

function fmtTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch (e) { return iso; }
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function renderItems(items) {
  grid.innerHTML = '';
  if (!items.length) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  for (const it of items) {
    const card = document.createElement('div');
    card.className = 'card';
    const time = fmtTime(it.ts);
    const app = it.session_app || '';
    const topic = it.session_topic || '';
    const motivation = it.session_motivation || '';
    const hasSession = !!(app || topic);
    const motivationHtml = hasSession
      ? (motivation
          ? `<div class="motivation">${escapeHtml(motivation)}</div>`
          : `<div class="motivation empty-msg">(noch keine Beschreibung)</div>`)
      : `<div class="no-session">(nicht in Session aggregiert)</div>`;

    card.innerHTML = `
      <img class="thumb" loading="lazy" src="${it.url}" alt="${escapeHtml(it.filename)}">
      <div class="meta">
        <div class="meta-row1">
          <span class="time">${time}</span>
          <span class="app">${escapeHtml(app)}</span>
        </div>
        ${topic ? `<div class="topic">${escapeHtml(topic)}</div>` : ''}
        ${motivationHtml}
      </div>
    `;
    card.querySelector('.thumb').addEventListener('click', () => openLightbox(it));
    grid.appendChild(card);
  }
}

function openLightbox(it) {
  lightboxImg.src = it.url;
  const time = fmtTime(it.ts);
  const app = escapeHtml(it.session_app || '');
  const topic = escapeHtml(it.session_topic || '');
  const motivation = escapeHtml(it.session_motivation || '');
  lightboxInfo.innerHTML = `
    <span class="lb-time">${time}</span>
    ${app ? `<span class="lb-app">${app}</span>` : ''}
    ${topic ? `<div class="lb-topic">${topic}</div>` : ''}
    ${motivation ? `<div class="lb-mot">${motivation}</div>` : ''}
  `;
  lightbox.classList.add('active');
}

lightbox.addEventListener('click', () => lightbox.classList.remove('active'));
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') lightbox.classList.remove('active');
});

async function loadDate(date) {
  selected = date;
  $('#dp-label').textContent = fmtDateLabel(date);
  renderCalendar();
  grid.innerHTML = '<div class="wt-loading" style="grid-column:1/-1"><div class="wt-spinner"></div><span class="wt-load-label">Screenshots laden…</span></div>';
  empty.style.display = 'none';
  try {
    const res = await fetch(`/api/screenshots/${date}`);
    const data = await res.json();
    renderItems(data.items || []);
    summary.textContent = `${(data.items || []).length} Screenshots`;
  } catch (e) {
    grid.innerHTML = '';
    empty.textContent = 'Fehler beim Laden: ' + e.message;
    empty.style.display = 'block';
  }
}

// ── Datepicker (only days with screenshots are clickable) ──
const MONTHS = ['Januar','Februar','März','April','Mai','Juni','Juli',
                'August','September','Oktober','November','Dezember'];
let available = new Set();   // 'YYYY-MM-DD'
let selected = null;
let viewY, viewM;            // month currently shown in the popover

function fmtDateLabel(d) {
  const [y, m, dd] = d.split('-');
  return `${dd}.${m}.${y}`;
}

function renderCalendar() {
  $('#dp-title').textContent = `${MONTHS[viewM]} ${viewY}`;
  const first = new Date(viewY, viewM, 1);
  const offset = (first.getDay() + 6) % 7;      // Monday-first
  const daysInMonth = new Date(viewY, viewM + 1, 0).getDate();
  let cells = '';
  for (let i = 0; i < offset; i++) cells += '<div class="dp-day dp-empty"></div>';
  const tn = new Date();
  const todayIso = `${tn.getFullYear()}-${String(tn.getMonth()+1).padStart(2,'0')}-${String(tn.getDate()).padStart(2,'0')}`;
  for (let day = 1; day <= daysInMonth; day++) {
    const iso = `${viewY}-${String(viewM+1).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
    const has = available.has(iso);
    const sel = iso === selected;
    const cls = 'dp-day' + (has ? ' has' : ' muted') + (sel ? ' sel' : '')
      + (iso === todayIso ? ' today' : '');
    cells += `<div class="${cls}"${has ? ` data-d="${iso}"` : ''}>${day}</div>`;
  }
  const gridEl = $('#dp-grid');
  gridEl.innerHTML = cells;
  gridEl.querySelectorAll('.dp-day.has').forEach(el =>
    el.addEventListener('click', () => {
      $('#dp-pop').hidden = true;
      loadDate(el.dataset.d);
    })
  );
  // Disable nav buttons beyond the available range
  const minIso = available.size ? [...available].sort()[0] : null;
  const maxIso = available.size ? [...available].sort().slice(-1)[0] : null;
  $('#dp-prev').disabled = minIso ? (viewY*12+viewM) <= (+minIso.slice(0,4)*12 + (+minIso.slice(5,7)-1)) : true;
  $('#dp-next').disabled = maxIso ? (viewY*12+viewM) >= (+maxIso.slice(0,4)*12 + (+maxIso.slice(5,7)-1)) : true;
}

function shiftMonth(delta) {
  viewM += delta;
  if (viewM < 0) { viewM = 11; viewY--; }
  else if (viewM > 11) { viewM = 0; viewY++; }
  renderCalendar();
}

$('#dp-trigger').addEventListener('click', (e) => {
  e.stopPropagation();
  const pop = $('#dp-pop');
  if (pop.hidden && selected) {
    viewY = +selected.slice(0,4); viewM = +selected.slice(5,7) - 1;
    renderCalendar();
  }
  pop.hidden = !pop.hidden;
});
$('#dp-prev').addEventListener('click', (e) => { e.stopPropagation(); shiftMonth(-1); });
$('#dp-next').addEventListener('click', (e) => { e.stopPropagation(); shiftMonth(1); });
$('#dp-pop').addEventListener('click', (e) => e.stopPropagation());
document.addEventListener('click', () => { $('#dp-pop').hidden = true; });

async function init() {
  try {
    const res = await fetch('/api/screenshots/dates');
    const dates = await res.json();
    if (!dates.length) {
      summary.textContent = 'Noch keine Screenshots aufgenommen.';
      empty.style.display = 'block';
      $('#dp-trigger').disabled = true;
      return;
    }
    available = new Set(dates);
    const latest = [...dates].sort().slice(-1)[0];
    viewY = +latest.slice(0,4); viewM = +latest.slice(5,7) - 1;
    loadDate(latest);
  } catch (e) {
    summary.textContent = 'Fehler: ' + e.message;
  }
}

init();
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════
# Config-Editor — `wt config` öffnet /config (Label/Value-Editor)
# ═══════════════════════════════════════════════════════════════════════════

AGG_LABELS = (
    "com.peab.worktracker.aggregator.daily",
    "com.peab.worktracker.aggregator.weekly",
    "com.peab.worktracker.aggregator.monthly",
)
# Keys that may not be written via the config editor API.
READONLY_CONFIG_PATHS = frozenset({"author", "version"})
COL_LABEL = "com.peab.worktracker.collector"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


def _cfg_get(d, path):
    for p in path.split("."):
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def _cfg_set(d, path, value):
    parts = path.split(".")
    for p in parts[:-1]:
        nxt = d.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            d[p] = nxt
        d = nxt
    d[parts[-1]] = value


def _cfg_coerce(value, default):
    """Coerce an incoming JSON value to the type suggested by the default."""
    if default is None:
        return value
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip()
        return int(s) if s else 0
    if isinstance(default, float):
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        return float(s) if s else 0.0
    if isinstance(default, list):
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [ln.strip() for ln in value.splitlines() if ln.strip()]
        return list(value) if value is not None else []
    if isinstance(default, dict):
        if isinstance(value, dict):
            return value
        raise ValueError("dict expected")
    # string default
    if value is None:
        return ""
    return str(value)


def _cfg_write_atomic(data: dict) -> None:
    _ensure_user_config()
    tmp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    os.replace(tmp, CONFIG_FILE)


def _cfg_load_pair():
    """Return (default_cfg, user_cfg). Bootstraps user file if missing."""
    _ensure_user_config()
    try:
        default = yaml.safe_load(CONFIG_DEFAULT_FILE.read_text()) or {}
    except Exception:
        default = {}
    try:
        user = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    except Exception:
        user = {}
    return default, user


def _safe_path_under_home(p: str) -> Path | None:
    try:
        resolved = Path(p).expanduser().resolve()
    except Exception:
        return None
    home = Path.home().resolve()
    try:
        resolved.relative_to(home)
    except ValueError:
        return None
    return resolved


@app.route("/api/config", methods=["GET"])
def api_config_get():
    default, user = _cfg_load_pair()
    return jsonify({
        "default": default,
        "user": user,
        "config_path": str(CONFIG_FILE),
        "config_default_path": str(CONFIG_DEFAULT_FILE),
    })


@app.route("/api/config/open-file", methods=["POST"])
def api_config_open_file():
    """Open config.yaml or config.default.yaml in the default editor."""
    body = request.get_json(silent=True) or {}
    which = body.get("which")
    if which == "user":
        _ensure_user_config()
        path = CONFIG_FILE
    elif which == "default":
        path = CONFIG_DEFAULT_FILE
    else:
        return jsonify({"ok": False, "error": "which must be 'user' or 'default'"}), 400
    if not path.exists():
        return jsonify({"ok": False, "error": f"does not exist: {path}"}), 404
    subprocess.Popen(["open", str(path)])
    return jsonify({"ok": True, "path": str(path)})


@app.route("/api/config", methods=["POST"])
def api_config_set():
    body = request.get_json(silent=True) or {}
    updates = body.get("updates")
    if updates is None:
        if "path" not in body:
            return jsonify({"ok": False, "error": "missing 'path' or 'updates'"}), 400
        updates = [{"path": body["path"], "value": body.get("value")}]
    if not isinstance(updates, list):
        return jsonify({"ok": False, "error": "'updates' must be a list"}), 400

    default, user = _cfg_load_pair()
    try:
        for u in updates:
            path = u.get("path")
            if not isinstance(path, str) or not path:
                return jsonify({"ok": False, "error": "invalid path"}), 400
            if path in READONLY_CONFIG_PATHS:
                return jsonify({"ok": False, "error": f"'{path}' is read-only"}), 403
            default_value = _cfg_get(default, path)
            coerced = _cfg_coerce(u.get("value"), default_value)
            _cfg_set(user, path, coerced)
        _cfg_write_atomic(user)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "user": user})


@app.route("/api/config/pick-path", methods=["POST"])
def api_config_pick_path():
    body = request.get_json(silent=True) or {}
    title = str(body.get("title") or "Verzeichnis wählen")
    start_path = body.get("start_path")
    # Escape for AppleScript double-quoted strings
    title_esc = title.replace("\\", "\\\\").replace('"', '\\"')
    if start_path:
        try:
            start_resolved = Path(str(start_path)).expanduser().resolve()
        except Exception:
            start_resolved = None
    else:
        start_resolved = None
    if start_resolved and start_resolved.is_dir():
        sp_esc = str(start_resolved).replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'POSIX path of (choose folder with prompt "{title_esc}" '
            f'default location (POSIX file "{sp_esc}"))'
        )
    else:
        script = f'POSIX path of (choose folder with prompt "{title_esc}")'
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "timeout"}), 504
    if proc.returncode != 0:
        # User cancel → exit code 1, stderr contains "User canceled."
        if "User canceled" in (proc.stderr or ""):
            return jsonify({"ok": True, "cancelled": True})
        return jsonify({"ok": False, "error": proc.stderr.strip() or "osascript failed"}), 500
    picked = proc.stdout.strip().rstrip("/")
    # Replace home prefix with ~ for nicer display/storage
    home = str(Path.home())
    if picked == home:
        picked = "~"
    elif picked.startswith(home + "/"):
        picked = "~" + picked[len(home):]
    return jsonify({"ok": True, "path": picked})


@app.route("/api/config/reveal", methods=["POST"])
def api_config_reveal():
    body = request.get_json(silent=True) or {}
    raw = body.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return jsonify({"ok": False, "error": "missing path"}), 400
    safe = _safe_path_under_home(raw)
    if safe is None:
        return jsonify({"ok": False, "error": "path must be under $HOME"}), 403
    if not safe.exists():
        # Try to create parent for nicer UX when the target dir is not yet there
        return jsonify({"ok": False, "error": f"does not exist: {safe}"}), 404
    subprocess.Popen(["open", str(safe)])
    return jsonify({"ok": True, "path": str(safe)})


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


@app.route("/api/config/restart/collector", methods=["POST"])
def api_config_restart_collector():
    plist = LAUNCH_AGENTS / f"{COL_LABEL}.plist"
    log = []
    _run(["launchctl", "stop", COL_LABEL])
    _run(["launchctl", "unload", str(plist)])
    rc1, o1 = _run(["launchctl", "load", str(plist)])
    log.append(o1)
    rc2, o2 = _run(["launchctl", "start", COL_LABEL])
    log.append(o2)
    ok = rc1 == 0 and rc2 == 0
    return jsonify({"ok": ok, "output": "\n".join(x for x in log if x)})


@app.route("/api/config/restart/aggregator", methods=["POST"])
def api_config_restart_aggregator():
    out = []
    all_ok = True
    for label in AGG_LABELS:
        plist = LAUNCH_AGENTS / f"{label}.plist"
        _run(["launchctl", "unload", str(plist)])
        rc, o = _run(["launchctl", "load", str(plist)])
        out.append(f"{label}: rc={rc} {o.strip()}")
        if rc != 0:
            all_ok = False
    return jsonify({"ok": all_ok, "output": "\n".join(out)})


# Map dashboard service keys → launchd labels. The collector is a long-running
# daemon (needs explicit start/stop); the aggregators are scheduled jobs where
# load/unload adds/removes them from the schedule.
_SERVICE_LABELS = {
    "collector": COL_LABEL,
    "agg_daily": "com.peab.worktracker.aggregator.daily",
    "agg_weekly": "com.peab.worktracker.aggregator.weekly",
    "agg_monthly": "com.peab.worktracker.aggregator.monthly",
}
_DAEMON_KEYS = frozenset({"collector"})


@app.route("/api/service/<key>/<action>", methods=["POST"])
def api_service_action(key, action):
    """Start / stop / restart a single launchd service.

    For the collector (a daemon) start/stop also kick/halt the process; for the
    scheduled aggregators load/unload toggles their presence in the schedule.
    """
    label = _SERVICE_LABELS.get(key)
    if not label or action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "unknown service or action"}), 400

    plist = LAUNCH_AGENTS / f"{label}.plist"
    is_daemon = key in _DAEMON_KEYS
    out = []
    rcs = []

    def step(cmd):
        rc, o = _run(cmd)
        if o.strip():
            out.append(o.strip())
        rcs.append(rc)
        return rc

    if action == "stop":
        if is_daemon:
            step(["launchctl", "stop", label])
        step(["launchctl", "unload", str(plist)])
        ok = rcs[-1] == 0
    elif action == "start":
        step(["launchctl", "load", str(plist)])
        ok = rcs[-1] == 0
        if is_daemon:
            ok = step(["launchctl", "start", label]) == 0 and ok
    else:  # restart
        if is_daemon:
            step(["launchctl", "stop", label])
        step(["launchctl", "unload", str(plist)])
        ok = step(["launchctl", "load", str(plist)]) == 0
        if is_daemon:
            ok = step(["launchctl", "start", label]) == 0 and ok

    return jsonify({"ok": ok, "output": "\n".join(out)})


# ---------------------------------------------------------------------------
# Projekt-Editor — /projects (liest/schreibt daemon/project_patterns.yaml)
# ---------------------------------------------------------------------------

PROJECT_RULE_KEYS = ("patterns", "url_patterns", "directories", "git_repos", "commands", "topics")


def _patterns_load_user() -> dict:
    try:
        return yaml.safe_load(PATTERNS_FILE.read_text()) or {}
    except Exception:
        return {}


_PATTERNS_HEADER = (
    "# WorkTracker — Benutzer-Projektpatterns.\n"
    "# Wird vom Web-Dashboard (/projects) verwaltet; handgeschriebene\n"
    "# Kommentare überleben ein Speichern dort nicht. Die letzte Version\n"
    "# liegt als project_patterns.yaml.bak daneben.\n"
)


def _patterns_write_atomic(data: dict) -> None:
    if PATTERNS_FILE.exists():
        bak = PATTERNS_FILE.with_suffix(PATTERNS_FILE.suffix + ".bak")
        bak.write_text(PATTERNS_FILE.read_text())
    tmp = PATTERNS_FILE.with_suffix(PATTERNS_FILE.suffix + ".tmp")
    tmp.write_text(_PATTERNS_HEADER + yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True,
        default_flow_style=None, width=4096,
    ))
    os.replace(tmp, PATTERNS_FILE)
    # Zuordnungen im URL-Panel hängen an den url_patterns → Cache verwerfen
    _VISITED_URLS_CACHE.clear()


def _clean_rule_list(value) -> list:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out


@app.route("/api/categories")
def api_categories():
    """Zentraler Kategorie-Pool (categories.default.yaml + categories.yaml)."""
    pool = catpool.load_pool()
    return jsonify({
        "activity": pool["activity"],
        "tool": pool["tool"],
        "alias_map": pool["alias_map"],
        "activity_names": catpool.activity_names(),
    })


@app.route("/api/projects", methods=["GET"])
def api_projects_get():
    data = _patterns_load_user()
    projects = data.get("projects") or {}
    if not isinstance(projects, dict):
        projects = {}
    # Pool-Kategorien + tatsächlich verwendete (kanonisiert) als Vorschläge
    in_use = {catpool.canonical(str(p.get("category"))) for p in projects.values()
              if isinstance(p, dict) and p.get("category")}
    categories = sorted(in_use | set(catpool.activity_names()), key=str.lower)
    return jsonify({
        "projects": projects,
        "categories": categories,
        "patterns_path": str(PATTERNS_FILE),
    })


@app.route("/api/projects", methods=["POST"])
def api_projects_save():
    body = request.get_json(silent=True) or {}
    name = _clean_name(str(body.get("name") or "")).strip()
    original = str(body.get("original_name") or "")
    if not name:
        return jsonify({"ok": False, "error": "Name darf nicht leer sein"}), 400

    data = _patterns_load_user()
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return jsonify({"ok": False, "error": "projects-Sektion ist kein Mapping"}), 500

    is_rename = bool(original) and original != name
    if name in projects and (is_rename or not original):
        return jsonify({"ok": False, "error": f"Projekt „{name}“ existiert bereits"}), 409

    # Bestehenden Eintrag übernehmen, damit unbekannte Keys (z. B.
    # clipboard_keywords) einen Rename/Edit überleben.
    entry = {}
    if original and isinstance(projects.get(original), dict):
        entry = projects.pop(original)

    rules = body.get("rules") or {}
    for key in PROJECT_RULE_KEYS:
        vals = _clean_rule_list(rules.get(key))
        if vals:
            entry[key] = vals
        else:
            entry.pop(key, None)

    category = str(body.get("category") or "").strip()
    if category:
        entry["category"] = category
    else:
        entry.pop("category", None)

    if not any(entry.get(k) for k in PROJECT_RULE_KEYS):
        return jsonify({"ok": False, "error": "Mindestens eine Regel angeben"}), 400

    projects[name] = entry
    _patterns_write_atomic(data)
    return jsonify({"ok": True, "name": name, "project": entry})


@app.route("/api/projects/delete", methods=["POST"])
def api_projects_delete():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "")
    data = _patterns_load_user()
    projects = data.get("projects") or {}
    if name not in projects:
        return jsonify({"ok": False, "error": f"unbekanntes Projekt: {name}"}), 404
    del projects[name]
    _patterns_write_atomic(data)
    return jsonify({"ok": True})


@app.route("/api/projects/match", methods=["POST"])
def api_projects_match():
    """Live-Tester: welche Projekte matchen einen Fenstertitel / eine URL?

    Spiegelt die Aggregator-Reihenfolge: erst alle Titel-Patterns (Tier 7),
    dann alle URL-Patterns (Tier 8) — der erste Treffer in der Liste ist
    der Gewinner.
    """
    body = request.get_json(silent=True) or {}
    text = _clean_name(str(body.get("text") or "")).strip().lower()
    if not text:
        return jsonify({"matches": []})
    projects = _patterns_load_user().get("projects") or {}
    matches = []
    seen = set()
    for field in ("patterns", "url_patterns"):
        for proj_name, info in projects.items():
            if proj_name in seen or not isinstance(info, dict):
                continue
            for pat in info.get(field) or []:
                if fnmatch.fnmatch(text, str(pat).lower()):
                    matches.append({"project": proj_name, "field": field, "pattern": pat})
                    seen.add(proj_name)
                    break
    return jsonify({"matches": matches})


def _tool_url_hosts() -> set:
    """Union der tool_app_url_hosts aus Default- und User-Patterns."""
    hosts = set()
    for path in (PATTERNS_DEFAULT_FILE, PATTERNS_FILE):
        try:
            data = yaml.safe_load(path.read_text()) or {}
            hosts.update(str(h).lower() for h in data.get("tool_app_url_hosts") or [])
        except Exception:
            continue
    return hosts


def _ignored_sets() -> tuple[set, set]:
    """(ignored_topics, ignored_url_hosts) aus den User-Patterns, lowercased."""
    data = _patterns_load_user()
    topics = {str(t).strip().lower() for t in data.get("ignored_topics") or [] if str(t).strip()}
    hosts = {str(h).strip().lower() for h in data.get("ignored_url_hosts") or [] if str(h).strip()}
    return topics, hosts


@app.route("/api/projects/ignore", methods=["POST"])
def api_projects_ignore():
    """Topic oder URL-Host dauerhaft aus den Vorschlagslisten ausblenden.

    Schreibt nach project_patterns.yaml unter `ignored_topics` bzw.
    `ignored_url_hosts`. Wirkt serverseitig in den /visited-urls- und
    /recognized-topics-Endpoints."""
    body = request.get_json(silent=True) or {}
    kind = str(body.get("type") or "").strip().lower()
    value = str(body.get("value") or "").strip()
    if not value:
        return jsonify({"ok": False, "error": "value fehlt"}), 400
    if kind == "url":
        key, norm = "ignored_url_hosts", value.lower()
    elif kind == "topic":
        key, norm = "ignored_topics", value
    else:
        return jsonify({"ok": False, "error": "type muss 'url' oder 'topic' sein"}), 400

    data = _patterns_load_user()
    lst = data.setdefault(key, [])
    if not isinstance(lst, list):
        lst = []
        data[key] = lst
    if not any(str(x).strip().lower() == norm.lower() for x in lst):
        lst.append(norm)
    _patterns_write_atomic(data)
    _RECOGNIZED_TOPICS_CACHE.clear()
    return jsonify({"ok": True, "type": kind, "value": norm})


# (days → (timestamp, payload)) — der Scan liest mehrere Tages-JSONLs vom
# (externen) Datenvolume, daher kurz gecacht.
_VISITED_URLS_CACHE: dict = {}
_VISITED_URLS_TTL = 60


@app.route("/api/projects/visited-urls")
def api_projects_visited_urls():
    """Besuchte Browser-Hosts der letzten N Tage, mit Projekt-Zuordnung."""
    try:
        days = max(1, min(60, int(request.args.get("days", 7))))
    except ValueError:
        days = 7

    now = datetime.now().timestamp()
    cached = _VISITED_URLS_CACHE.get(days)
    if cached and now - cached[0] < _VISITED_URLS_TTL:
        return jsonify(cached[1])

    from collections import Counter
    from urllib.parse import urlparse

    counts: Counter = Counter()
    samples: dict = {}
    for i in range(days):
        path = DATA_SNAP / f"{(datetime.now() - timedelta(days=i)):%Y-%m-%d}.jsonl"
        if not path.exists():
            continue
        try:
            with open(path) as f:
                for line in f:
                    if '"url"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    aa = d.get("active_app") or {}
                    url = aa.get("url")
                    if not url:
                        continue
                    host = urlparse(url).netloc.lower()
                    if host.startswith("www."):
                        host = host[4:]
                    # Eigene Dashboards/Dev-Server sind kein Browsing-Signal
                    if not host or host.startswith(("127.0.0.1", "localhost")):
                        continue
                    counts[host] += 1
                    samples.setdefault(host, (url, aa.get("window_title") or ""))
        except Exception:
            continue

    interval = 10
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text()) or {}
        interval = int(cfg.get("collector", {}).get("interval_seconds", 10)) or 10
    except Exception:
        pass

    projects = _patterns_load_user().get("projects") or {}
    tool_hosts = _tool_url_hosts()
    _, ignored_hosts = _ignored_sets()

    def assigned_to(host: str, sample_url: str):
        if host in tool_hosts:
            return "(Tool-App)"
        u = sample_url.lower()
        for proj_name, info in projects.items():
            if not isinstance(info, dict):
                continue
            for pat in info.get("url_patterns") or []:
                p = str(pat).lower()
                if fnmatch.fnmatch(u, p) or fnmatch.fnmatch(host, p):
                    return proj_name
        return None

    def project_category(proj_name):
        info = projects.get(proj_name) if proj_name else None
        if isinstance(info, dict):
            return catpool.canonical(str(info.get("category") or "")) or None
        return None

    urls = []
    for host, count in counts.most_common(80):
        if host in ignored_hosts:
            continue
        sample_url, sample_title = samples[host]
        proj = assigned_to(host, sample_url)
        web_main, web_sub = classify_url(sample_url)
        web_cat = web_main if web_main and web_main != "Other" else None
        urls.append({
            "host": host,
            "count": count,
            "minutes": round(count * interval / 60),
            "sample_url": sample_url[:300],
            "sample_title": sample_title[:200],
            "project": proj,
            "project_category": project_category(proj),
            "web_category": web_cat,
        })

    payload = {"urls": urls, "days": days}
    _VISITED_URLS_CACHE[days] = (now, payload)
    return jsonify(payload)


_RECOGNIZED_TOPICS_CACHE: dict = {}
_RECOGNIZED_TOPICS_TTL = 60


@app.route("/api/projects/recognized-topics")
def api_projects_recognized_topics():
    """Vom LLM erkannte Topics der letzten N Tage, mit Projekt-Zuordnung.

    Spiegelt das „Besuchte URLs“-Panel: jeder Eintrag zeigt, wie viel Zeit
    auf das Topic entfiel, welches Projekt aktuell dominiert und ob das Topic
    bereits per `topics`-Regel einem Projekt fest zugeordnet ist.
    """
    try:
        days = max(1, min(60, int(request.args.get("days", 7))))
    except ValueError:
        days = 7

    now = datetime.now().timestamp()
    cached = _RECOGNIZED_TOPICS_CACHE.get(days)
    if cached and now - cached[0] < _RECOGNIZED_TOPICS_TTL:
        return jsonify(cached[1])

    from collections import Counter

    secs: Counter = Counter()
    proj_secs: dict = {}   # topic -> Counter(project -> sec) (beobachtete Zuordnung)
    app_cat_secs: dict = {}  # topic -> Counter(app_category -> sec)
    act_secs: dict = {}    # topic -> Counter(activity_category -> sec) (Pool)
    longs: dict = {}       # topic -> topic_long (Beispiel)
    for i in range(days):
        date_str = f"{(datetime.now() - timedelta(days=i)):%Y-%m-%d}"
        for s in load_sessions(date_str):
            topic = (s.get("topic") or "").strip()
            if not topic:
                continue
            dur = int(s.get("duration_seconds", 0) or 0)
            secs[topic] += dur
            proj = (s.get("project") or "").strip()
            if proj and proj.lower() != "other":
                proj_secs.setdefault(topic, Counter())[proj] += dur
            app_cat = (s.get("app_category") or "").strip()
            if app_cat and app_cat.lower() != "other":
                app_cat_secs.setdefault(topic, Counter())[app_cat] += dur
            act = (s.get("activity_category") or "").strip()
            if act and act.lower() != "other":
                act_secs.setdefault(topic, Counter())[act] += dur
            if topic not in longs and s.get("topic_long"):
                longs[topic] = str(s.get("topic_long"))[:240]

    # Feste Zuordnung aus project_patterns.yaml (topics-Regel je Projekt)
    projects = _patterns_load_user().get("projects") or {}
    ignored_topics, _ = _ignored_sets()
    assigned: dict = {}   # topic-glob/lower -> project (assignment rules)
    for proj_name, info in projects.items():
        if not isinstance(info, dict):
            continue
        for pat in info.get("topics") or []:
            assigned[str(pat).lower()] = proj_name

    def assigned_to(topic: str):
        t = topic.lower()
        for pat, proj_name in assigned.items():
            if fnmatch.fnmatch(t, pat):
                return proj_name
        return None

    def project_category(proj_name):
        info = projects.get(proj_name) if proj_name else None
        if isinstance(info, dict):
            return catpool.canonical(str(info.get("category") or "")) or None
        return None

    topics = []
    for topic, sec in secs.most_common(80):
        if topic.lower() in ignored_topics:
            continue
        observed = proj_secs.get(topic)
        dominant = observed.most_common(1)[0][0] if observed else None
        rule_proj = assigned_to(topic)
        app_cats = app_cat_secs.get(topic)
        app_cat = app_cats.most_common(1)[0][0] if app_cats else None
        acts = act_secs.get(topic)
        proj_cat = project_category(rule_proj) or project_category(dominant)
        topics.append({
            "topic": topic,
            "minutes": round(sec / 60),
            "topic_long": longs.get(topic, ""),
            "project": rule_proj,                 # feste Regel-Zuordnung
            "observed_project": dominant,         # aktuell dominierendes Projekt
            # Projekt-Kategorie: aus Regel-Zuordnung, sonst aus beobachtetem Projekt
            "project_category": proj_cat,
            "app_category": app_cat,              # dominante App-Kategorie (Web/App)
            # Pool-Kategorie: Projekt-Kategorie, sonst dominante abgeleitete
            # Aktivitäts-Kategorie der Sessions (Web → Tool-Brücke)
            "category": proj_cat or (acts.most_common(1)[0][0] if acts else None),
        })

    payload = {"topics": topics, "days": days}
    _RECOGNIZED_TOPICS_CACHE[days] = (now, payload)
    return jsonify(payload)


@app.route("/projects")
def projects_editor():
    return render_template_string(PROJECTS_HTML)


@app.route("/config")
def config_editor():
    return render_template_string(CONFIG_HTML)


CONFIG_HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>WorkTracker — Config</title>
<style>
  :root {
    /* Palette mirrors docs/index.html so /config matches the documentation skin */
    --bg: #0c1116;       /* docs --bg */
    --panel: #141a20;    /* docs --bg2 */
    --panel2: #1b222a;   /* docs --bg3 */
    --fg: #cfd7e0;       /* docs --text */
    --muted: #8a95a3;    /* docs --text2 */
    --accent: #d4f500;   /* docs --acid (primary highlight) */
    --accent2: #4fc3d8;  /* docs --cyan (secondary accent, hover/focus) */
    --ok: #6fe28a;       /* docs --green */
    --warn: #ff5a1f;     /* docs --orange */
    --err: #ff4d4f;      /* docs --red */
    --border: #2b3642;   /* docs --border */
    --white: #f4f8ff;    /* docs --white */
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
    background: var(--bg); color: var(--fg);
  }
  header {
    position: sticky; top: 0; z-index: 10;
    background: rgba(12,17,22,.92); backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border);
    padding: 12px 20px; display: flex; align-items: center; gap: 14px;
  }
  header h1 { margin: 0; font-size: 18px; font-weight: 600; letter-spacing: .2px; color: var(--accent); }
  header h1 a { color: var(--muted); font-size: 12px; text-decoration: none; font-weight: 400; }
  header h1 a:hover { color: var(--accent); }
  header .path { color: var(--muted); font-size: 12px; font-family: ui-monospace, Menlo, monospace; }
  header .spacer { flex: 1; }
  button {
    background: var(--panel2); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 12px; font-size: 13px; cursor: pointer;
    transition: background .12s, border-color .12s;
    display: inline-flex; align-items: center; gap: 7px;
  }
  button svg { flex: 0 0 auto; display: block; }
  button:hover { background: var(--border); border-color: var(--accent2); color: var(--white); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #0c1116; font-weight: 600; }
  button.primary:hover { filter: brightness(1.08); color: #0c1116; }
  button.danger { border-color: var(--err); }
  button.danger:hover { background: rgba(255,77,79,.12); border-color: var(--err); color: var(--err); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  main { max-width: 1760px; margin: 0 auto; padding: 18px clamp(16px, 2.5vw, 36px) 80px; }
  /* Navbar auf dieselbe Breite wie der Inhalt begrenzen (nicht volle Bildschirmbreite),
     analog zu Explore/Statistics, wo sie im 1760px-Body steckt. */
  body > .wt-nav { max-width: 1760px; margin-left: auto; margin-right: auto; }
  section { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 18px; overflow: hidden; }
  section > h2 {
    margin: 0; padding: 12px 18px; font-size: 13px; text-transform: uppercase;
    letter-spacing: 1.2px; color: var(--muted); border-bottom: 1px solid var(--border);
    background: var(--panel2);
  }
  .row {
    display: grid; grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
    gap: 18px; padding: 12px 18px; align-items: center;
    border-bottom: 1px solid rgba(127,144,160,.12);
  }
  .row:last-child { border-bottom: none; }
  .row .label {
    font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; color: var(--fg);
    overflow-wrap: anywhere; word-break: break-word; line-height: 1.4;
  }
  .row .label .seg-dim { color: var(--muted); }
  /* Label as deep-link into the documentation for that value. */
  a.label.doc-link {
    display: block; text-decoration: none; color: inherit; cursor: pointer;
    border-radius: 5px; margin: -3px -6px; padding: 3px 6px;
    transition: background .12s, color .12s;
  }
  a.label.doc-link .doc-ico {
    color: var(--muted); margin-left: 6px; font-size: 11px;
    opacity: 0; transition: opacity .12s;
  }
  a.label.doc-link:hover { background: rgba(79,195,216,.10); }
  a.label.doc-link:hover .seg-dim,
  a.label.doc-link:hover span { color: var(--accent2); }
  a.label.doc-link:hover .doc-ico { opacity: 1; color: var(--accent2); }
  .row .label .sub { display: block; color: var(--muted); font-size: 11px; margin-top: 2px; font-family: inherit; }
  .row .control { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; min-width: 0; }
  .row .control > input[type=text],
  .row .control > textarea { flex: 1 1 260px; min-width: 0; max-width: 640px; }
  input[type=text], input[type=number], textarea {
    background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 5px;
    padding: 7px 10px; font: 13px ui-monospace, Menlo, monospace;
    outline: none; box-sizing: border-box;
  }
  input[type=number] { flex: 0 0 140px; max-width: 160px; }
  input[type=text]:focus, input[type=number]:focus, textarea:focus {
    border-color: var(--accent2);
    box-shadow: 0 0 0 1px var(--accent2);
  }
  input[readonly], textarea[readonly] {
    color: var(--muted); background: var(--panel); cursor: default;
    border-color: rgba(127,144,160,.12);
  }
  input[readonly]:focus, textarea[readonly]:focus { border-color: rgba(127,144,160,.22); }
  textarea { width: 100%; min-height: 84px; resize: vertical; line-height: 1.45; white-space: pre; overflow-x: auto; }
  @media (max-width: 720px) {
    .row { grid-template-columns: 1fr; gap: 8px; padding: 12px 14px; }
    .row .label { font-size: 12px; }
    .row .control > input[type=text],
    .row .control > textarea { max-width: 100%; flex-basis: 100%; }
  }
  /* Standalone toolbar below the navbar (path + open/restart buttons). */
  .cfg-toolbar {
    max-width: 1760px; margin: 0 auto; padding: 14px clamp(16px, 2.5vw, 36px) 0;
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }
  .cfg-toolbar .path {
    color: var(--muted); font-size: 12px;
    font-family: ui-monospace, Menlo, monospace;
    overflow-wrap: anywhere; word-break: break-word;
  }
  .cfg-toolbar-actions {
    margin-left: auto; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  @media (max-width: 720px) {
    .cfg-toolbar { padding: 12px 14px 0; }
    .cfg-toolbar-actions { margin-left: 0; }
  }
  .docs-link-wrap {
    display: flex; justify-content: center;
    margin: 6px 0 22px;
  }
  .docs-card {
    display: inline-flex; align-items: center; gap: 10px;
    padding: 10px 20px;
    background: linear-gradient(180deg, rgba(212,245,0,.10), rgba(212,245,0,.03));
    border: 1px solid rgba(212,245,0,.35); border-radius: 999px;
    color: var(--accent); text-decoration: none;
    font-size: 15px; font-weight: 600; letter-spacing: .2px;
    transition: border-color .15s, background .15s, color .15s,
                transform .08s, box-shadow .2s;
  }
  .docs-card:hover {
    border-color: var(--accent); background: var(--accent);
    color: #0c1116;
    box-shadow: 0 4px 18px rgba(212,245,0,.28);
  }
  .docs-card:active { transform: translateY(1px); box-shadow: 0 2px 8px rgba(212,245,0,.2); }
  .docs-card .book { width: 17px; height: 17px; flex: none; }
  .docs-card .arrow {
    display: inline-block; font-weight: 700;
    transition: transform .15s;
  }
  .docs-card:hover .arrow { transform: translateX(3px); }
  .switch { position: relative; width: 38px; height: 22px; display: inline-block; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute; inset: 0; background: var(--panel2); border-radius: 22px;
    cursor: pointer; transition: background .15s;
    border: 1px solid var(--border);
  }
  .slider::before {
    content: ""; position: absolute; left: 2px; top: 2px;
    width: 16px; height: 16px; background: var(--muted); border-radius: 50%;
    transition: transform .15s, background .15s;
  }
  .switch input:checked + .slider { background: var(--accent); border-color: var(--accent); }
  .switch input:checked + .slider::before { transform: translateX(16px); background: #0c1116; }
  .toast {
    position: fixed; bottom: 18px; right: 20px; padding: 8px 14px;
    background: var(--panel2); border: 1px solid var(--border); border-radius: 8px;
    font-size: 12px; opacity: 0; transform: translateY(6px);
    transition: opacity .15s, transform .15s; pointer-events: none;
    max-width: 420px;
  }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.ok { border-color: var(--ok); }
  .toast.err { border-color: var(--err); }
  .hint { color: var(--muted); font-size: 11.5px; padding: 6px 16px 10px; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); margin-right: 6px; }
  .status-dot.saving { background: var(--warn); }
  .status-dot.ok { background: var(--ok); }
  .status-dot.err { background: var(--err); }
  .unit { color: var(--muted); font-size: 12px; margin-left: 4px; }
</style>
</head>
<body>
@@NAV:config@@
@@CFG_TOOLBAR@@
<main id="main">
  <div class="wt-loading" id="loadingMsg"><div class="wt-spinner"></div><span class="wt-load-label">Lade Konfiguration…</span></div>
</main>
<div id="toast" class="toast"></div>

<script>
const PATH_KEY_HINTS = /_dir$|_path$|^dir$|^path$|^endpoint$/;

const UNITS = {
  "interval_seconds": "s",
  "git_scan_interval_seconds": "s",
  "idle_threshold_seconds": "s",
  "focus_session_min_seconds": "s",
  "same_app_grace_period_seconds": "s",
  "timeout_seconds": "s",
  "min_session_seconds": "s",
  "threshold_minutes": "min",
  "cooldown_minutes": "min",
  "deep_work_min_minutes": "min",
  "image_max_bytes": "bytes",
};

function tlabel(k) {
  // Use dotted path as-is — that IS the label per spec
  return k;
}

function unitFor(path) {
  const leaf = path.split(".").pop();
  return UNITS[leaf] || "";
}

function isPathField(path, defaultValue) {
  if (typeof defaultValue !== "string") return false;
  const leaf = path.split(".").pop();
  if (PATH_KEY_HINTS.test(leaf)) return true;
  // Heuristic: starts with ~ or /
  if (defaultValue.startsWith("~") || defaultValue.startsWith("/")) return true;
  return false;
}

let toastTimer = null;
function toast(msg, kind) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast show " + (kind || "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast " + (kind || ""); }, 2400);
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
  let j = null;
  try { j = await r.json(); } catch (_) {}
  if (!r.ok || (j && j.ok === false)) {
    const msg = (j && j.error) || r.statusText;
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return j;
}

async function saveField(path, value) {
  try {
    await postJSON("/api/config", {path, value});
    toast("gespeichert · " + path, "ok");
  } catch (e) {
    toast("Fehler: " + e.message, "err");
    throw e;
  }
}

const READONLY_PATHS = new Set(["author", "version"]);

function renderDocsCard() {
  const wrap = document.createElement("div");
  wrap.className = "docs-link-wrap";
  const a = document.createElement("a");
  a.href = "/docs/index.html#config";
  a.target = "_blank";
  a.rel = "noopener";
  a.className = "docs-card";
  a.innerHTML =
    '<svg class="book" viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>' +
    '<path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>' +
    'Config Documentation <span class="arrow">&rsaquo;</span>';
  wrap.appendChild(a);
  return wrap;
}

function renderScalar(path, defaultValue, userValue) {
  const control = document.createElement("div");
  control.className = "control";
  const current = userValue === undefined ? defaultValue : userValue;
  const readonly = READONLY_PATHS.has(path);

  if (typeof defaultValue === "boolean") {
    const wrap = document.createElement("label");
    wrap.className = "switch";
    const inp = document.createElement("input");
    inp.type = "checkbox"; inp.checked = !!current;
    if (readonly) inp.disabled = true;
    const sl = document.createElement("span"); sl.className = "slider";
    wrap.appendChild(inp); wrap.appendChild(sl);
    if (!readonly) inp.addEventListener("change", () => saveField(path, inp.checked));
    control.appendChild(wrap);
    return control;
  }

  if (typeof defaultValue === "number") {
    const inp = document.createElement("input");
    inp.type = "number";
    if (Number.isInteger(defaultValue)) inp.step = "1"; else inp.step = "any";
    inp.value = current ?? 0;
    if (readonly) inp.readOnly = true;
    else inp.addEventListener("change", () => {
      const v = inp.value === "" ? 0 : Number(inp.value);
      saveField(path, v);
    });
    control.appendChild(inp);
    const u = unitFor(path);
    if (u) { const s = document.createElement("span"); s.className = "unit"; s.textContent = u; control.appendChild(s); }
    return control;
  }

  // string
  const inp = document.createElement("input");
  inp.type = "text";
  inp.value = current ?? "";
  if (readonly) inp.readOnly = true;
  let lastSaved = inp.value;
  const save = () => {
    if (inp.value === lastSaved) return;
    lastSaved = inp.value;
    saveField(path, inp.value);
  };
  if (!readonly) {
    inp.addEventListener("blur", save);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); inp.blur(); } });
  }
  control.appendChild(inp);

  if (!readonly && isPathField(path, defaultValue)) {
    const browse = document.createElement("button");
    browse.textContent = "Browse…";
    browse.addEventListener("click", async () => {
      try {
        const r = await postJSON("/api/config/pick-path", {title: "Wähle: " + path, start_path: inp.value});
        if (r.cancelled) return;
        inp.value = r.path;
        lastSaved = r.path;
        await saveField(path, r.path);
      } catch (e) { toast("Fehler: " + e.message, "err"); }
    });
    control.appendChild(browse);

    const open = document.createElement("button");
    open.textContent = "Open";
    open.title = "Im Finder öffnen";
    open.addEventListener("click", async () => {
      try {
        await postJSON("/api/config/reveal", {path: inp.value});
      } catch (e) { toast("Fehler: " + e.message, "err"); }
    });
    control.appendChild(open);
  }
  return control;
}

function renderList(path, defaultValue, userValue) {
  const control = document.createElement("div");
  control.className = "control";
  const current = Array.isArray(userValue) ? userValue : (Array.isArray(defaultValue) ? defaultValue : []);
  const ta = document.createElement("textarea");
  ta.placeholder = "Ein Eintrag pro Zeile";
  ta.value = current.join("\n");
  let lastSaved = ta.value;
  ta.addEventListener("blur", () => {
    if (ta.value === lastSaved) return;
    lastSaved = ta.value;
    const arr = ta.value.split("\n").map(s => s.trim()).filter(s => s);
    saveField(path, arr);
  });
  control.appendChild(ta);
  return control;
}

function renderRow(path, defaultValue, userValue) {
  const row = document.createElement("div");
  row.className = "row";
  // Meta keys (version/author) aren't documented \u2014 keep them as plain labels.
  const linkable = !READONLY_PATHS.has(path);
  const lab = document.createElement(linkable ? "a" : "div");
  lab.className = "label" + (linkable ? " doc-link" : "");
  if (linkable) {
    lab.href = "/docs/index.html#cfg-" + path;
    lab.target = "_blank";
    lab.rel = "noopener";
    lab.title = "Doku \u00F6ffnen: " + path;
  } else {
    lab.title = path;
  }
  // Segment the dotted path: dim parents + soft break hint, emphasize leaf
  const parts = path.split(".");
  const leaf = parts.pop();
  if (parts.length) {
    const dim = document.createElement("span");
    dim.className = "seg-dim";
    // Insert zero-width space after each dot so the browser prefers to break there
    dim.textContent = parts.join(".\u200B") + ".\u200B";
    lab.appendChild(dim);
  }
  const strong = document.createElement("span");
  strong.textContent = leaf;
  lab.appendChild(strong);
  if (linkable) {
    const ico = document.createElement("span");
    ico.className = "doc-ico";
    ico.textContent = "\u2197";
    ico.setAttribute("aria-hidden", "true");
    lab.appendChild(ico);
  }
  row.appendChild(lab);

  let ctrl;
  if (Array.isArray(defaultValue)) {
    ctrl = renderList(path, defaultValue, userValue);
  } else {
    ctrl = renderScalar(path, defaultValue, userValue);
  }
  row.appendChild(ctrl);
  return row;
}

function walk(defaultObj, userObj, prefix, rows) {
  for (const [k, v] of Object.entries(defaultObj)) {
    const path = prefix ? prefix + "." + k : k;
    const userV = (userObj && typeof userObj === "object") ? userObj[k] : undefined;
    if (v && typeof v === "object" && !Array.isArray(v)) {
      walk(v, (userV && typeof userV === "object") ? userV : {}, path, rows);
    } else {
      rows.push(renderRow(path, v, userV));
    }
  }
}

async function load() {
  const main = document.getElementById("main");
  main.innerHTML = '<div class="wt-loading" id="loadingMsg"><div class="wt-spinner"></div><span class="wt-load-label">Lade Konfiguration…</span></div>';
  let data;
  try {
    const r = await fetch("/api/config");
    data = await r.json();
  } catch (e) {
    main.innerHTML = '<div class="hint" style="color:var(--err)">Fehler beim Laden: ' + e.message + "</div>";
    return;
  }
  document.getElementById("cfgPath").textContent = data.config_path || "";
  main.innerHTML = "";

  const topKeys = Object.keys(data.default || {});
  // Skip scalar top-level meta keys (version, author) — render as own section
  const metaKeys = topKeys.filter(k => typeof data.default[k] !== "object" || data.default[k] === null);
  const sectionKeys = topKeys.filter(k => typeof data.default[k] === "object" && data.default[k] !== null && !Array.isArray(data.default[k]));

  if (metaKeys.length) {
    const sec = document.createElement("section");
    const h = document.createElement("h2"); h.textContent = "meta"; sec.appendChild(h);
    for (const k of metaKeys) {
      sec.appendChild(renderRow(k, data.default[k], data.user ? data.user[k] : undefined));
    }
    main.appendChild(sec);
  }

  // Docs link card — rendered directly after meta
  main.appendChild(renderDocsCard());

  for (const sk of sectionKeys) {
    const sec = document.createElement("section");
    const h = document.createElement("h2"); h.textContent = sk; sec.appendChild(h);
    const hint = document.createElement("div");
    hint.className = "hint";
    if (sk === "collector") hint.textContent = 'Änderungen werden erst nach "Restart Collector" aktiv.';
    else if (sk === "aggregator") hint.textContent = 'Änderungen werden beim nächsten Aggregator-Lauf wirksam (oder sofort via "Restart Aggregator").';
    if (hint.textContent) sec.appendChild(hint);
    const rows = [];
    walk(data.default[sk], (data.user && data.user[sk]) || {}, sk, rows);
    for (const row of rows) sec.appendChild(row);
    main.appendChild(sec);
  }

  // Second docs link — at the very bottom of the page
  main.appendChild(renderDocsCard());
}

async function doRestart(which, btn) {
  const lab = btn.querySelector(".btn-label") || btn;
  const orig = lab.textContent;
  btn.disabled = true; lab.textContent = "…restarting";
  try {
    const r = await postJSON("/api/config/restart/" + which, {});
    toast(which + " neu gestartet", "ok");
  } catch (e) {
    toast("Restart fehlgeschlagen: " + e.message, "err");
  } finally {
    btn.disabled = false; lab.textContent = orig;
  }
}

document.getElementById("btnRestartCol").addEventListener("click", (e) => doRestart("collector", e.currentTarget));
document.getElementById("btnRestartAgg").addEventListener("click", (e) => doRestart("aggregator", e.currentTarget));

async function openCfgFile(which, btn) {
  btn.disabled = true;
  try {
    const r = await postJSON("/api/config/open-file", {which});
    toast("geöffnet: " + (r.path || which), "ok");
  } catch (e) {
    toast("Fehler: " + e.message, "err");
  } finally {
    btn.disabled = false;
  }
}
document.getElementById("btnOpenDefault").addEventListener("click", (e) => openCfgFile("default", e.currentTarget));
document.getElementById("btnOpenUser").addEventListener("click", (e) => openCfgFile("user", e.currentTarget));

load();
</script>
</body>
</html>"""


PROJECTS_HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>WorkTracker — Projects/Topics</title>
<style>
  :root {
    /* Palette mirrors docs/index.html (same skin as /config) */
    --bg: #0c1116; --panel: #141a20; --panel2: #1b222a;
    --fg: #cfd7e0; --muted: #8a95a3;
    --accent: #d4f500; --accent2: #4fc3d8;
    --ok: #6fe28a; --warn: #ff5a1f; --err: #ff4d4f;
    --border: #2b3642; --white: #f4f8ff; --violet: #c792ea;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
    background: var(--bg); color: var(--fg);
  }
  button {
    background: var(--panel2); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 6px 12px; font-size: 13px; cursor: pointer;
    transition: background .12s, border-color .12s;
    display: inline-flex; align-items: center; gap: 7px;
  }
  button svg { flex: 0 0 auto; display: block; }
  button:hover { background: var(--border); border-color: var(--accent2); color: var(--white); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #0c1116; font-weight: 600; }
  button.primary:hover { filter: brightness(1.08); color: #0c1116; }
  button.danger { border-color: var(--err); }
  button.danger:hover { background: rgba(255,77,79,.12); border-color: var(--err); color: var(--err); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  main { max-width: 1760px; margin: 0 auto; padding: 18px clamp(16px, 2.5vw, 36px) 80px; }
  /* Navbar auf dieselbe Breite wie der Inhalt begrenzen (nicht volle Bildschirmbreite),
     analog zu Explore/Statistics, wo sie im 1760px-Body steckt. */
  body > .wt-nav { max-width: 1760px; margin-left: auto; margin-right: auto; }
  .toolbar {
    max-width: 1760px; margin: 0 auto; padding: 14px clamp(16px, 2.5vw, 36px) 0;
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }
  .toolbar .path {
    color: var(--muted); font-size: 12px;
    font-family: ui-monospace, Menlo, monospace; overflow-wrap: anywhere;
  }
  .toolbar-actions { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; }
  .hint { color: var(--muted); font-size: 11.5px; margin: 0 0 16px; }

  /* --- Regel-Tester ------------------------------------------------------ */
  .tester {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 18px; margin-bottom: 16px;
  }
  .tester input[type=text] {
    width: 100%; background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 9px 12px; font: 13px ui-monospace, Menlo, monospace; outline: none;
  }
  .tester input[type=text]:focus { border-color: var(--accent2); box-shadow: 0 0 0 1px var(--accent2); }
  .test-result { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; min-height: 0; }
  .test-result:empty { margin-top: 0; }
  .test-win { background: var(--accent); color: #0c1116; font-weight: 600; padding: 3px 12px; border-radius: 999px; font-size: 12.5px; }
  .test-detail { color: var(--muted); font: 12px ui-monospace, Menlo, monospace; }
  .test-also { border: 1px solid var(--border); color: var(--muted); padding: 2px 10px; border-radius: 999px; font-size: 12px; }
  .test-none { color: var(--muted); font-size: 12.5px; }

  /* --- Besuchte URLs (Drag-Quelle) ---------------------------------------- */
  .urls-panel {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 18px; margin-bottom: 16px;
  }
  .urls-head { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 4px; }
  .urls-head .ed-label { margin-bottom: 0; }
  .urls-days { display: flex; gap: 4px; }
  .day-pill {
    padding: 3px 11px; border-radius: 999px; border: 1px solid var(--border);
    cursor: pointer; font-size: 11.5px; color: var(--muted); background: var(--panel2);
    transition: border-color .12s, background .12s;
  }
  .day-pill:hover { border-color: var(--accent2); }
  .day-pill.active { background: var(--accent); color: #0c1116; border-color: var(--accent); font-weight: 600; }
  .urls-toggle {
    margin-left: auto; display: inline-flex; align-items: center; gap: 6px;
    font-size: 11.5px; color: var(--muted); cursor: pointer; user-select: none;
  }
  .urls-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .url-chip {
    display: inline-flex; align-items: center; gap: 7px;
    font: 12px ui-monospace, Menlo, monospace;
    padding: 4px 6px 4px 11px; border-radius: 999px;
    border: 1px solid rgba(79,195,216,.4); color: var(--accent2);
    background: rgba(79,195,216,.06); cursor: grab; max-width: 100%;
  }
  .url-chip:active { cursor: grabbing; }
  .url-chip .u-host { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .url-chip .u-count { color: var(--muted); font-size: 11px; }
  .url-chip .u-add {
    cursor: pointer; font-weight: 700; color: var(--muted);
    border: 1px solid var(--border); border-radius: 50%;
    width: 17px; height: 17px; line-height: 15px; text-align: center; flex: 0 0 auto;
    font-family: -apple-system, system-ui, sans-serif;
  }
  .url-chip .u-add:hover { color: #0c1116; background: var(--accent); border-color: var(--accent); }
  /* „X“ — Topic/Host dauerhaft ignorieren */
  .url-chip .u-del {
    cursor: pointer; font-weight: 700; color: var(--muted);
    border: 1px solid var(--border); border-radius: 50%;
    width: 17px; height: 17px; line-height: 15px; text-align: center; flex: 0 0 auto;
    font-family: -apple-system, system-ui, sans-serif;
  }
  .url-chip .u-del:hover { color: #0c1116; background: var(--err, #ff5555); border-color: var(--err, #ff5555); }
  /* Kategorie-Badges: Projekt-Kategorie (lila) vs. Web/App-Kategorie (grau-blau) */
  .cat-badge {
    font: 10px ui-monospace, Menlo, monospace; padding: 1px 6px;
    border-radius: 999px; flex: 0 0 auto; white-space: nowrap;
  }
  .cat-badge.cat-proj { color: #bd93f9; border: 1px solid rgba(189,147,249,.45); background: rgba(189,147,249,.08); }
  .cat-badge.cat-web  { color: var(--muted); border: 1px solid var(--border); background: transparent; }
  .url-chip.assigned { border-color: var(--border); color: var(--muted); background: transparent; opacity: .75; }
  .url-chip.assigned .u-proj { font-size: 10.5px; color: var(--ok); }
  .url-chip.dragging { opacity: .4; }
  /* Topic-Chips: gleiche Mechanik wie URL-Chips, aber pinker Akzent */
  .topic-chip { border-color: rgba(255,121,198,.45); color: #ff79c6; background: rgba(255,121,198,.06); }
  .topic-chip .u-obs { color: var(--muted); font-size: 10.5px; }
  .url-chip .u-proj.clickable, .url-chip .u-obs.clickable {
    cursor: pointer; border-radius: 999px; padding: 0 6px; transition: background .12s, color .12s;
  }
  .url-chip .u-proj.clickable:hover, .url-chip .u-obs.clickable:hover {
    background: var(--accent); color: #0c1116;
  }
  /* Drop-Targets während eines URL-Drags */
  body.dragging-url .card,
  body.dragging-topic .card { outline: 1px dashed rgba(79,195,216,.45); outline-offset: 2px; }
  body.dragging-url .card.editor,
  body.dragging-topic .card.editor { outline: none; }
  .card.drop-hover { border-color: var(--accent) !important; box-shadow: 0 0 0 2px var(--accent); }
  body.dragging-url #btnNew { outline: 2px dashed var(--accent); outline-offset: 3px; }
  #btnNew.drop-hover { filter: brightness(1.2); box-shadow: 0 0 0 2px var(--accent); }
  /* Kategorie-Überschrift als Drop-Ziel */
  body.dragging-url .cat-head {
    border-radius: 6px; padding: 2px 6px; margin-left: -6px;
    outline: 1px dashed rgba(79,195,216,.45);
  }
  .cat-head.cat-drop-hover { outline: 1px solid var(--accent); color: var(--accent); }
  /* „Neue Kategorie“-Drop-Zone — nur während eines URL-Drags sichtbar */
  .newcat-drop {
    display: none; margin-top: 8px; padding: 14px; border-radius: 10px;
    border: 1px dashed var(--border); color: var(--muted);
    text-align: center; font-size: 12.5px;
  }
  body.dragging-url .newcat-drop { display: block; border-color: var(--accent2); color: var(--accent2); }
  .newcat-drop.drop-hover { border-color: var(--accent); color: var(--accent); box-shadow: 0 0 0 2px var(--accent); }

  /* --- Kopfzeile mit Suche + Neu ----------------------------------------- */
  .controls { display: flex; gap: 10px; margin-bottom: 6px; align-items: center; flex-wrap: wrap; }
  .controls input[type=search] {
    flex: 1 1 240px; background: var(--bg); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 12px; font-size: 13px; outline: none;
  }
  .controls input[type=search]:focus { border-color: var(--accent2); }

  /* --- Kategorie-Gruppen + Karten ---------------------------------------- */
  .cat-group { margin-bottom: 24px; }
  .cat-head {
    display: flex; align-items: center; gap: 8px; margin: 0 0 10px;
    font-size: 12px; text-transform: uppercase; letter-spacing: 1.2px; color: var(--muted);
  }
  .cat-dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-left: 3px solid var(--border); border-radius: 10px;
    padding: 12px 14px; cursor: pointer; min-width: 0;
    transition: border-color .12s, box-shadow .12s, opacity .15s;
  }
  .card:hover { border-color: var(--accent2); }
  .card.winner { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .card.hit { border-color: var(--accent2); }
  .card.dim { opacity: .35; }
  .card-name { font-weight: 600; color: var(--white); font-size: 14.5px; overflow-wrap: anywhere; }
  .card-chips { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 5px; }
  .chip {
    font: 11.5px ui-monospace, Menlo, monospace; padding: 2px 9px; border-radius: 999px;
    border: 1px solid var(--border); color: var(--muted); background: rgba(127,144,160,.05);
    max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .chip.t-patterns     { border-color: rgba(212,245,0,.35);  color: rgba(212,245,0,.85); }
  .chip.t-url_patterns { border-color: rgba(79,195,216,.4);  color: var(--accent2); }
  .chip.t-directories  { border-color: rgba(111,226,138,.4); color: var(--ok); }
  .chip.t-git_repos    { border-color: rgba(255,90,31,.45);  color: #ff8a5c; }
  .chip.t-commands     { border-color: rgba(199,146,234,.45); color: var(--violet); }
  .chip.t-topics       { border-color: rgba(255,121,198,.45); color: #ff79c6; }
  /* Light Mode: kräftigere Chip-Farben, sonst auf Weiß kaum lesbar */
  html[data-theme="light"] .chip.t-patterns  { border-color: rgba(132,163,0,.55); color: #6b8400; }
  html[data-theme="light"] .chip.t-git_repos { border-color: rgba(192,106,16,.5); color: #c06a10; }

  /* --- Editor-Karte ------------------------------------------------------- */
  .card.editor { grid-column: 1 / -1; cursor: default; border-color: var(--accent2); margin-bottom: 12px; }
  .ed-label {
    font-size: 11.5px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px;
    display: flex; gap: 8px; align-items: center; margin-bottom: 7px;
  }
  .ed-label .dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
  .rule-dot.t-patterns     { background: var(--accent); }
  .rule-dot.t-url_patterns { background: var(--accent2); }
  .rule-dot.t-directories  { background: var(--ok); }
  .rule-dot.t-git_repos    { background: #ff8a5c; }
  .rule-dot.t-commands     { background: var(--violet); }
  .rule-dot.t-topics       { background: #ff79c6; }
  .ed-section { margin-top: 16px; }
  .ed-name {
    width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 9px 12px; color: var(--white); font-size: 16px; font-weight: 600; outline: none;
  }
  .ed-name:focus { border-color: var(--accent2); box-shadow: 0 0 0 1px var(--accent2); }
  .cat-pills { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .cat-pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 12px; border-radius: 999px; border: 1px solid var(--border);
    cursor: pointer; font-size: 12.5px; color: var(--fg); background: var(--panel2);
    transition: border-color .12s, background .12s;
  }
  .cat-pill:hover { border-color: var(--accent2); }
  .cat-pill.active { background: var(--accent); color: #0c1116; border-color: var(--accent); font-weight: 600; }
  .cat-pill .dot { width: 8px; height: 8px; border-radius: 50%; }
  .cat-new {
    background: var(--bg); border: 1px dashed var(--border); border-radius: 999px;
    padding: 4px 12px; color: var(--fg); font-size: 12.5px; outline: none; width: 160px;
  }
  .cat-new:focus { border-color: var(--accent2); border-style: solid; }
  .chips-edit {
    display: flex; flex-wrap: wrap; gap: 6px; padding: 8px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    min-height: 42px; align-items: center; cursor: text;
  }
  .chips-edit:focus-within { border-color: var(--accent2); }
  .chips-edit .chip { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; padding: 3px 7px 3px 10px; }
  .chip .x { cursor: pointer; opacity: .55; font-weight: 700; padding: 0 3px; }
  .chip .x:hover { opacity: 1; color: var(--err); }
  .chip .edit { cursor: pointer; opacity: .55; padding: 0 2px; font-size: 11px; }
  .chip .edit:hover { opacity: 1; color: var(--accent2); }
  .chip-input {
    flex: 1 1 140px; min-width: 140px; background: transparent; border: none; outline: none;
    color: var(--fg); font: 12.5px ui-monospace, Menlo, monospace; padding: 4px;
  }
  .rule-hint { font-size: 11px; color: var(--muted); margin-top: 5px; }
  .ed-footer { display: flex; gap: 10px; margin-top: 20px; align-items: center; }
  .ed-footer .spacer { flex: 1; }

  .toast {
    position: fixed; bottom: 18px; right: 20px; padding: 8px 14px;
    background: var(--panel2); border: 1px solid var(--border); border-radius: 8px;
    font-size: 12px; opacity: 0; transform: translateY(6px);
    transition: opacity .15s, transform .15s; pointer-events: none; max-width: 420px;
  }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.ok { border-color: var(--ok); }
  .toast.err { border-color: var(--err); }
  @media (max-width: 720px) {
    main { padding: 14px 14px 80px; }
    .toolbar { padding: 12px 14px 0; }
    .toolbar-actions { margin-left: 0; }
  }
</style>
</head>
<body>
@@NAV:projects@@
<div class="toolbar">
  <span class="path" id="patPath"></span>
  <div class="toolbar-actions">
    <button id="btnRestartAgg" class="primary" title="Aggregator-Jobs neu laden">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
      <span class="btn-label">Restart Aggregator</span></button>
  </div>
</div>
<main>
  <div class="tester">
    <div class="ed-label">Regel-Tester</div>
    <input type="text" id="testInput" autocomplete="off" spellcheck="false"
           placeholder="Fenstertitel oder URL eintippen — zeigt sofort, welches Projekt gewinnen würde…">
    <div class="test-result" id="testResult"></div>
  </div>
  <div class="urls-panel">
    <div class="urls-head">
      <div class="ed-label">Besuchte URLs</div>
      <div class="urls-days" id="urlsDays">
        <span class="day-pill" data-days="1">Heute</span>
        <span class="day-pill active" data-days="7">7 Tage</span>
        <span class="day-pill" data-days="30">30 Tage</span>
      </div>
      <label class="urls-toggle"><input type="checkbox" id="showAssigned"> bereits zugeordnete anzeigen</label>
    </div>
    <div class="urls-list" id="urlsList"><span class="hint" style="margin:0">Lade besuchte URLs…</span></div>
    <div class="rule-hint">Ziehe eine URL auf ein Projekt, um sie dort als URL-Regel zu speichern — oder ziehe sie auf eine Kategorie bzw. auf „＋ Neue Kategorie“, um daraus ein neues Projekt zu machen.</div>
  </div>
  <div class="urls-panel">
    <div class="urls-head">
      <div class="ed-label">Erkannte Topics</div>
      <div class="urls-days" id="topicsDays">
        <span class="day-pill" data-days="1">Heute</span>
        <span class="day-pill active" data-days="7">7 Tage</span>
        <span class="day-pill" data-days="30">30 Tage</span>
      </div>
      <label class="urls-toggle"><input type="checkbox" id="showAssignedTopics"> bereits zugeordnete anzeigen</label>
    </div>
    <div class="urls-list" id="topicsList"><span class="hint" style="margin:0">Lade Topics…</span></div>
    <div class="rule-hint">Ziehe ein Topic auf ein Projekt, um es dort als Topic-Regel zu speichern. Sessions mit diesem Topic, die sonst unter „Other“ landen, werden dann diesem Projekt zugeordnet.</div>
  </div>
  <div class="controls">
    <input type="search" id="searchInput" placeholder="Projekte filtern…">
    <button id="btnNew" class="primary">+ Neues Projekt</button>
  </div>
  <div class="hint">Klicke ein Projekt, um Name, Kategorie und Regeln zu bearbeiten. Änderungen landen in project_patterns.yaml und wirken ab dem nächsten Aggregator-Lauf.</div>
  <div id="groups"><div class="wt-loading"><div class="wt-spinner"></div><span class="wt-load-label">Lade Projekte…</span></div></div>
</main>
<div id="toast" class="toast"></div>

<script>
const RULE_TYPES = [
  {key: "patterns",     label: "Fenstertitel",     ph: "z. B. worktracker",  cls: "t-patterns",
   hint: "Matcht den Fenstertitel. * steht für beliebigen Text — Eingaben ohne * werden automatisch zu *eingabe*."},
  {key: "url_patterns", label: "URLs",             ph: "z. B. github.com",   cls: "t-url_patterns",
   hint: "Matcht die Browser-URL."},
  {key: "directories",  label: "Verzeichnisse",    ph: "z. B. worktracker",  cls: "t-directories",
   hint: "Ordnernamen — erkannt im Terminal-Arbeitsverzeichnis, in der IDE und im Finder."},
  {key: "git_repos",    label: "Git-Repos",        ph: "z. B. WorkTracker",  cls: "t-git_repos",
   hint: "Repository-Namen aus dem Git-Signal des Collectors."},
  {key: "commands",     label: "Terminal-Befehle", ph: "z. B. wt *",         cls: "t-commands",
   hint: "Befehle aus dem Terminal-Titel."},
  {key: "topics",       label: "Topics",           ph: "z. B. Steuererklärung", cls: "t-topics",
   hint: "Vom LLM erkanntes Topic. Greift nachrangig: ordnet sonst „Other“-Sessions diesem Projekt zu, wenn ihr Topic passt. * als Platzhalter erlaubt."},
];

// Kategorie-Farben aus dem zentralen Pool (/api/categories); die Konstanten
// hier sind nur Fallback, bis der Pool geladen ist.
let CAT_COLORS = {
  "Development": "#6fe28a", "Business": "#ffb347", "Creative": "#ff79c6",
  "Music": "#c792ea", "Crypto": "#f5d400", "Finance": "#4fc3d8",
  "AI": "#d4f500", "News": "#ff8a5c", "Communication": "#7aa2f7",
  "Social Media": "#5ab0f7", "Entertainment": "#ff5a8a", "Research": "#9ece6a",
};
let CAT_ALIAS = {};
function canonCat(cat) {
  if (!cat) return cat;
  return CAT_ALIAS[String(cat).toLowerCase()] || cat;
}
async function loadCatPool() {
  try {
    const d = await (await fetch("/api/categories")).json();
    const m = {};
    for (const [n, i] of Object.entries(d.tool || {})) if (i && i.color) m[n] = i.color;
    for (const [n, i] of Object.entries(d.activity || {})) if (i && i.color) m[n] = i.color;
    CAT_COLORS = m;
    CAT_ALIAS = d.alias_map || {};
  } catch (e) { /* Fallback-Farben bleiben */ }
}
function catColor(cat) {
  cat = canonCat(cat);
  if (!cat) return "#8a95a3";
  if (CAT_COLORS[cat]) return CAT_COLORS[cat];
  let h = 0;
  for (const ch of cat) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return "hsl(" + h + ", 65%, 62%)";
}

let DB = {projects: {}, categories: []};
let openName = null;   // null = kein Editor offen, "" = neues Projekt, sonst Projektname
let draft = null;
let dirty = false;
let filter = "";
let testMatches = [];
let URLS = [];
let urlDays = 7;
let draggingHost = null;
let TOPICS = [];
let topicDays = 7;
let draggingTopic = null;

let toastTimer = null;
function toast(msg, kind) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast show " + (kind || "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast " + (kind || ""); }, 2400);
}

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });
  let j = null;
  try { j = await r.json(); } catch (_) {}
  if (!r.ok || (j && j.ok === false)) {
    const err = new Error((j && j.error) || r.statusText);
    err.status = r.status;
    throw err;
  }
  return j;
}

function newDraft(name, info) {
  const rules = {};
  for (const rt of RULE_TYPES) {
    rules[rt.key] = (info && Array.isArray(info[rt.key])) ? info[rt.key].slice() : [];
  }
  return {original: name || "", name: name || "", category: (info && info.category) || "", rules};
}

function openEditor(name, preset) {
  if (openName !== null && dirty && !confirm("Ungespeicherte Änderungen verwerfen?")) return;
  openName = name;
  draft = newDraft(name, name ? DB.projects[name] : null);
  dirty = false;
  if (preset) {
    if (preset.name) draft.name = preset.name;
    if (preset.category) draft.category = preset.category;
    if (preset.rules) {
      for (const k of Object.keys(preset.rules)) draft.rules[k] = preset.rules[k].slice();
    }
    dirty = true;
  }
  render();
  const ni = document.getElementById("edName");
  if (ni) { ni.focus(); ni.scrollIntoView({block: "nearest", behavior: "smooth"}); }
}

function closeEditor(force) {
  if (dirty && !force && !confirm("Ungespeicherte Änderungen verwerfen?")) return;
  openName = null; draft = null; dirty = false;
  render();
}

async function saveDraft() {
  const body = {
    original_name: draft.original,
    name: draft.name.trim(),
    category: draft.category.trim(),
    rules: draft.rules,
  };
  try {
    await postJSON("/api/projects", body);
    toast("gespeichert · " + body.name, "ok");
    openName = null; draft = null; dirty = false;
    await load();
  } catch (e) { toast("Fehler: " + e.message, "err"); }
}

async function deleteProject() {
  if (!draft.original) { closeEditor(true); return; }
  if (!confirm("Projekt „" + draft.original + "“ wirklich löschen?")) return;
  try {
    await postJSON("/api/projects/delete", {name: draft.original});
    toast("gelöscht · " + draft.original, "ok");
    openName = null; draft = null; dirty = false;
    await load();
  } catch (e) { toast("Fehler: " + e.message, "err"); }
}

function buildChipsEdit(rt) {
  const wrap = document.createElement("div");
  wrap.className = "chips-edit";
  const inp = document.createElement("input");
  inp.className = "chip-input";
  inp.placeholder = rt.ph;
  inp.autocomplete = "off";
  inp.spellcheck = false;

  const renderChips = () => {
    wrap.querySelectorAll(".chip").forEach(c => c.remove());
    draft.rules[rt.key].forEach((val, i) => {
      const ch = document.createElement("span");
      ch.className = "chip " + rt.cls;
      const t = document.createElement("span");
      t.textContent = val;
      ch.appendChild(t);
      const ed = document.createElement("span");
      ed.className = "edit";
      ed.textContent = "✎";
      ed.title = "bearbeiten";
      ed.addEventListener("click", (e) => {
        e.stopPropagation();
        // Eintrag aus der Liste nehmen und zum Bearbeiten ins Eingabefeld holen.
        draft.rules[rt.key].splice(i, 1);
        dirty = true;
        inp.value = val;
        renderChips();
        inp.focus();
        inp.setSelectionRange(val.length, val.length);
      });
      ch.appendChild(ed);
      const x = document.createElement("span");
      x.className = "x";
      x.textContent = "×";
      x.title = "entfernen";
      x.addEventListener("click", (e) => {
        e.stopPropagation();
        draft.rules[rt.key].splice(i, 1);
        dirty = true;
        renderChips();
      });
      ch.appendChild(x);
      wrap.insertBefore(ch, inp);
    });
  };

  const add = () => {
    let v = inp.value.trim();
    if (!v) return;
    // Glob-Komfort: Titel/URL-Muster ohne Wildcard automatisch einrahmen
    if ((rt.key === "patterns" || rt.key === "url_patterns") && !/[*?\[]/.test(v)) {
      v = "*" + v + "*";
    }
    if (!draft.rules[rt.key].includes(v)) draft.rules[rt.key].push(v);
    dirty = true;
    inp.value = "";
    renderChips();
  };

  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); }
    else if (e.key === "Backspace" && !inp.value && draft.rules[rt.key].length) {
      draft.rules[rt.key].pop();
      dirty = true;
      renderChips();
    }
  });
  inp.addEventListener("blur", add);
  wrap.addEventListener("click", () => inp.focus());
  wrap.appendChild(inp);
  renderChips();
  return wrap;
}

function buildEditor() {
  const card = document.createElement("div");
  card.className = "card editor";

  const nameLab = document.createElement("div");
  nameLab.className = "ed-label";
  nameLab.textContent = draft.original ? "Projekt bearbeiten" : "Neues Projekt";
  card.appendChild(nameLab);
  const name = document.createElement("input");
  name.id = "edName";
  name.className = "ed-name";
  name.placeholder = "Projektname";
  name.value = draft.name;
  name.autocomplete = "off";
  name.spellcheck = false;
  name.addEventListener("input", () => { draft.name = name.value; dirty = true; });
  card.appendChild(name);

  // Kategorie als klickbare Pills + Freitext für neue Kategorien
  const catSec = document.createElement("div");
  catSec.className = "ed-section";
  const catLab = document.createElement("div");
  catLab.className = "ed-label";
  catLab.textContent = "Kategorie";
  catSec.appendChild(catLab);
  const pills = document.createElement("div");
  pills.className = "cat-pills";
  const cats = DB.categories.filter(c => c);
  const catInput = document.createElement("input");
  const refreshPills = () => {
    pills.querySelectorAll(".cat-pill").forEach(p => {
      p.classList.toggle("active", p.dataset.cat === draft.category);
    });
  };
  for (const c of cats) {
    const p = document.createElement("span");
    p.className = "cat-pill";
    p.dataset.cat = c;
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = catColor(c);
    p.appendChild(dot);
    p.appendChild(document.createTextNode(c));
    p.addEventListener("click", () => {
      draft.category = (draft.category === c) ? "" : c;
      dirty = true;
      catInput.value = "";
      refreshPills();
    });
    pills.appendChild(p);
  }
  catInput.type = "text";
  catInput.className = "cat-new";
  catInput.placeholder = "Neue Kategorie…";
  catInput.autocomplete = "off";
  if (draft.category && !cats.includes(draft.category)) catInput.value = draft.category;
  catInput.addEventListener("input", () => {
    draft.category = catInput.value.trim();
    dirty = true;
    refreshPills();
  });
  pills.appendChild(catInput);
  catSec.appendChild(pills);
  card.appendChild(catSec);
  refreshPills();

  for (const rt of RULE_TYPES) {
    const sec = document.createElement("div");
    sec.className = "ed-section";
    const lab = document.createElement("div");
    lab.className = "ed-label";
    const dot = document.createElement("span");
    dot.className = "dot rule-dot " + rt.cls;
    lab.appendChild(dot);
    lab.appendChild(document.createTextNode(rt.label));
    sec.appendChild(lab);
    sec.appendChild(buildChipsEdit(rt));
    const hint = document.createElement("div");
    hint.className = "rule-hint";
    hint.textContent = rt.hint;
    sec.appendChild(hint);
    card.appendChild(sec);
  }

  const foot = document.createElement("div");
  foot.className = "ed-footer";
  if (draft.original) {
    const del = document.createElement("button");
    del.className = "danger";
    del.textContent = "Löschen";
    del.addEventListener("click", deleteProject);
    foot.appendChild(del);
  }
  const sp = document.createElement("div");
  sp.className = "spacer";
  foot.appendChild(sp);
  const cancel = document.createElement("button");
  cancel.textContent = "Abbrechen";
  cancel.addEventListener("click", () => closeEditor(false));
  foot.appendChild(cancel);
  const save = document.createElement("button");
  save.className = "primary";
  save.textContent = "Speichern";
  save.addEventListener("click", saveDraft);
  foot.appendChild(save);
  card.appendChild(foot);

  // Drop auf den offenen Editor: URL/Topic als Entwurfs-Regel übernehmen (ohne Save)
  card.addEventListener("dragover", (e) => {
    if (!draggingHost && !draggingTopic) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  card.addEventListener("drop", (e) => {
    if (!draggingHost && !draggingTopic) return;
    e.preventDefault();
    if (draggingTopic) {
      if (!draft.rules.topics.includes(draggingTopic)) {
        draft.rules.topics.push(draggingTopic);
        dirty = true;
        render();
        toast("übernommen (noch nicht gespeichert) · " + draggingTopic, "ok");
      }
      return;
    }
    const pat = "*" + draggingHost + "*";
    if (!draft.rules.url_patterns.includes(pat)) {
      draft.rules.url_patterns.push(pat);
      dirty = true;
      render();
      toast("übernommen (noch nicht gespeichert) · " + pat, "ok");
    }
  });
  return card;
}

function buildCard(name, info) {
  const card = document.createElement("div");
  card.className = "card";
  card.style.borderLeftColor = catColor(info.category || "");
  const head = document.createElement("div");
  head.className = "card-name";
  head.textContent = name;
  card.appendChild(head);

  const chips = document.createElement("div");
  chips.className = "card-chips";
  const all = [];
  for (const rt of RULE_TYPES) {
    for (const v of (info[rt.key] || [])) all.push([String(v), rt.cls]);
  }
  const MAX = 7;
  for (const [v, cls] of all.slice(0, MAX)) {
    const ch = document.createElement("span");
    ch.className = "chip " + cls;
    ch.textContent = v;
    ch.title = v;
    chips.appendChild(ch);
  }
  if (all.length > MAX) {
    const m = document.createElement("span");
    m.className = "chip";
    m.textContent = "+" + (all.length - MAX);
    chips.appendChild(m);
  }
  if (!all.length) {
    const m = document.createElement("span");
    m.className = "chip";
    m.textContent = "keine Regeln";
    chips.appendChild(m);
  }
  card.appendChild(chips);

  const idx = testMatches.findIndex(m => m.project === name);
  if (idx === 0) card.classList.add("winner");
  else if (idx > 0) card.classList.add("hit");
  else if (testMatches.length) card.classList.add("dim");

  card.addEventListener("click", () => openEditor(name));

  // Drop-Target für URL- und Topic-Chips aus den Panels
  card.addEventListener("dragover", (e) => {
    if (!draggingHost && !draggingTopic) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    card.classList.add("drop-hover");
  });
  card.addEventListener("dragleave", () => card.classList.remove("drop-hover"));
  card.addEventListener("drop", (e) => {
    if (!draggingHost && !draggingTopic) return;
    e.preventDefault();
    card.classList.remove("drop-hover");
    if (draggingTopic) addTopicToProject(name, draggingTopic);
    else addUrlToProject(name, draggingHost);
  });
  return card;
}

function groupProjects() {
  const groups = new Map();
  for (const [name, info] of Object.entries(DB.projects)) {
    // Pool-kanonisiert gruppieren; Other/leer landet im Sammelbecken
    let cat = canonCat((info && info.category) || "");
    if (!cat || cat === "Other") cat = "Uncategorized";
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(name);
  }
  const keys = [...groups.keys()].sort((a, b) => {
    const ua = a === "Uncategorized", ub = b === "Uncategorized";
    if (ua !== ub) return ua ? 1 : -1;
    return a.localeCompare(b, "de", {sensitivity: "base"});
  });
  return keys.map(k => [k, groups.get(k).sort((a, b) => a.localeCompare(b, "de", {sensitivity: "base"}))]);
}

function passesFilter(name, info) {
  if (!filter) return true;
  const f = filter.toLowerCase();
  if (name.toLowerCase().includes(f)) return true;
  if (String(info.category || "").toLowerCase().includes(f)) return true;
  for (const rt of RULE_TYPES) {
    for (const v of (info[rt.key] || [])) {
      if (String(v).toLowerCase().includes(f)) return true;
    }
  }
  return false;
}

function render() {
  const root = document.getElementById("groups");
  root.innerHTML = "";
  if (openName === "" && draft) root.appendChild(buildEditor());

  let total = 0, shown = 0;
  for (const [cat, names] of groupProjects()) {
    total += names.length;
    const vis = names.filter(n => passesFilter(n, DB.projects[n] || {}));
    if (!vis.length) continue;
    shown += vis.length;
    const g = document.createElement("div");
    g.className = "cat-group";
    const h = document.createElement("div");
    h.className = "cat-head";
    const dot = document.createElement("span");
    dot.className = "cat-dot";
    dot.style.background = catColor(cat);
    h.appendChild(dot);
    h.appendChild(document.createTextNode((cat === "Uncategorized" ? "Ohne Kategorie" : cat) + " · " + vis.length));
    // Drop-Ziel: URL auf eine Kategorie ziehen → neues Projekt in dieser Kategorie
    h.addEventListener("dragover", (e) => {
      if (!draggingHost) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      h.classList.add("cat-drop-hover");
    });
    h.addEventListener("dragleave", () => h.classList.remove("cat-drop-hover"));
    h.addEventListener("drop", (e) => {
      if (!draggingHost) return;
      e.preventDefault();
      h.classList.remove("cat-drop-hover");
      newProjectFromHost(draggingHost, cat === "Uncategorized" ? "" : cat);
    });
    g.appendChild(h);
    const grid = document.createElement("div");
    grid.className = "grid";
    for (const n of vis) {
      if (openName === n && draft) grid.appendChild(buildEditor());
      else grid.appendChild(buildCard(n, DB.projects[n] || {}));
    }
    g.appendChild(grid);
    root.appendChild(g);
  }
  if (!shown && openName === null) {
    const e = document.createElement("div");
    e.className = "hint";
    e.textContent = total
      ? "Keine Projekte passen zum Filter."
      : "Noch keine Projekte — lege mit „+ Neues Projekt“ los.";
    root.appendChild(e);
  }
  // Drop-Zone „Neue Kategorie“ — nur während eines URL-Drags sichtbar (CSS)
  const ncz = document.createElement("div");
  ncz.className = "newcat-drop";
  ncz.textContent = "＋ Neue Kategorie aus URL";
  ncz.addEventListener("dragover", (e) => {
    if (!draggingHost) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    ncz.classList.add("drop-hover");
  });
  ncz.addEventListener("dragleave", () => ncz.classList.remove("drop-hover"));
  ncz.addEventListener("drop", (e) => {
    if (!draggingHost) return;
    e.preventDefault();
    ncz.classList.remove("drop-hover");
    newProjectFromHost(draggingHost, "");   // leere Kategorie → Freitextfeld bekommt Fokus
  });
  root.appendChild(ncz);
  const pc = document.getElementById("proj-count");
  if (pc) pc.textContent = total;
}

let testTimer = null;
async function runTest() {
  const text = document.getElementById("testInput").value.trim();
  const res = document.getElementById("testResult");
  if (!text) {
    testMatches = [];
    res.innerHTML = "";
    render();
    return;
  }
  try {
    const j = await postJSON("/api/projects/match", {text});
    testMatches = j.matches || [];
  } catch (e) {
    testMatches = [];
  }
  res.innerHTML = "";
  if (!testMatches.length) {
    const s = document.createElement("span");
    s.className = "test-none";
    s.textContent = "kein Treffer — würde unter „Other“ landen";
    res.appendChild(s);
  } else {
    const w = testMatches[0];
    const s = document.createElement("span");
    s.className = "test-win";
    s.textContent = "→ " + w.project;
    res.appendChild(s);
    const d = document.createElement("span");
    d.className = "test-detail";
    d.textContent = (w.field === "patterns" ? "Titel-Muster " : "URL-Muster ") + w.pattern;
    res.appendChild(d);
    for (const m of testMatches.slice(1, 5)) {
      const a = document.createElement("span");
      a.className = "test-also";
      a.textContent = m.project;
      a.title = m.pattern;
      res.appendChild(a);
    }
    if (testMatches.length > 5) {
      const a = document.createElement("span");
      a.className = "test-also";
      a.textContent = "+" + (testMatches.length - 5);
      res.appendChild(a);
    }
  }
  render();
}

document.getElementById("testInput").addEventListener("input", () => {
  clearTimeout(testTimer);
  testTimer = setTimeout(runTest, 250);
});
document.getElementById("searchInput").addEventListener("input", (e) => {
  filter = e.target.value.trim();
  render();
});
// ---- Besuchte URLs: Laden, Rendern, Drag & Drop ---------------------------

function suggestName(host) {
  // "business.apple.com" → "Business Apple", "korodrogerie.de" → "Korodrogerie"
  const parts = host.split(".");
  if (parts.length > 1) parts.pop();
  return parts
    .filter(p => p)
    .map(p => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ") || host;
}

async function addUrlToProject(projName, host) {
  const info = DB.projects[projName];
  if (!info) return;
  const rules = {};
  for (const rt of RULE_TYPES) {
    rules[rt.key] = Array.isArray(info[rt.key]) ? info[rt.key].slice() : [];
  }
  const pat = "*" + host + "*";
  if (rules.url_patterns.includes(pat)) {
    toast(pat + " ist bei „" + projName + "“ schon vorhanden", "err");
    return;
  }
  rules.url_patterns.push(pat);
  try {
    await postJSON("/api/projects", {
      original_name: projName, name: projName,
      category: info.category || "", rules,
    });
    toast(pat + " → " + projName, "ok");
    await load();
    await loadUrls();
  } catch (e) { toast("Fehler: " + e.message, "err"); }
}

async function addTopicToProject(projName, topic) {
  const info = DB.projects[projName];
  if (!info) return;
  const rules = {};
  for (const rt of RULE_TYPES) {
    rules[rt.key] = Array.isArray(info[rt.key]) ? info[rt.key].slice() : [];
  }
  if (rules.topics.includes(topic)) {
    toast("„" + topic + "“ ist bei „" + projName + "“ schon vorhanden", "err");
    return;
  }
  rules.topics.push(topic);
  try {
    await postJSON("/api/projects", {
      original_name: projName, name: projName,
      category: info.category || "", rules,
    });
    toast("„" + topic + "“ → " + projName, "ok");
    await load();
    await loadTopics();
  } catch (e) { toast("Fehler: " + e.message, "err"); }
}

function newProjectFromHost(host, category) {
  const pat = "*" + host + "*";
  const preset = {name: suggestName(host), rules: {patterns: [pat], url_patterns: [pat]}};
  if (category) preset.category = category;
  openEditor("", preset);
  window.scrollTo({top: 0, behavior: "smooth"});
  // Ohne vorgegebene Kategorie direkt ins Kategorie-Freitextfeld springen.
  if (!category) {
    setTimeout(() => {
      const ci = document.querySelector(".card.editor .cat-new");
      if (ci) ci.focus();
    }, 60);
  }
}

// Kategorie-Badge: kind = 'proj' (Projekt-Kategorie) | 'web' (Web/App-Kategorie)
function catBadge(text, kind) {
  const b = document.createElement("span");
  b.className = "cat-badge cat-" + kind;
  b.textContent = text;
  b.title = kind === "proj" ? "Projekt-Kategorie" : "Web/App-Kategorie";
  return b;
}

// „X“-Button: Topic/Host dauerhaft ignorieren (serverseitig persistiert)
function makeDeleteBtn(type, value, onDone) {
  const x = document.createElement("span");
  x.className = "u-del";
  x.textContent = "×";
  x.title = (type === "url" ? "Host" : "Topic") + " dauerhaft ausblenden";
  x.addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      await fetch("/api/projects/ignore", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({type, value}),
      });
    } catch (err) { /* trotzdem clientseitig ausblenden */ }
    onDone();
  });
  return x;
}

function renderUrls() {
  const list = document.getElementById("urlsList");
  const showAssigned = document.getElementById("showAssigned").checked;
  list.innerHTML = "";
  const visible = URLS.filter(u => showAssigned || !u.project);
  if (!visible.length) {
    const s = document.createElement("span");
    s.className = "hint";
    s.style.margin = "0";
    s.textContent = URLS.length
      ? "Alle besuchten URLs sind bereits einem Projekt zugeordnet."
      : "Keine besuchten URLs im Zeitraum gefunden.";
    list.appendChild(s);
    return;
  }
  for (const u of visible) {
    const chip = document.createElement("span");
    chip.className = "url-chip" + (u.project ? " assigned" : "");
    chip.draggable = true;
    const tparts = ["≈ " + (u.minutes < 1 ? "<1" : u.minutes) + " min im Browser"];
    if (u.sample_title) tparts.push("zuletzt: " + u.sample_title);
    if (u.project) tparts.push("zugeordnet: " + u.project);
    chip.title = tparts.join("\n");

    const host = document.createElement("span");
    host.className = "u-host";
    host.textContent = u.host;
    chip.appendChild(host);
    const cnt = document.createElement("span");
    cnt.className = "u-count";
    cnt.textContent = (u.minutes < 1 ? "<1" : u.minutes) + "m";
    chip.appendChild(cnt);
    if (u.project) {
      const pr = document.createElement("span");
      pr.className = "u-proj";
      pr.textContent = "→ " + u.project;
      chip.appendChild(pr);
    }
    if (u.project_category) chip.appendChild(catBadge(u.project_category, "proj"));
    if (u.web_category) chip.appendChild(catBadge(u.web_category, "web"));
    const add = document.createElement("span");
    add.className = "u-add";
    add.textContent = "+";
    add.title = "Neues Projekt aus " + u.host;
    add.addEventListener("click", (e) => { e.stopPropagation(); newProjectFromHost(u.host); });
    chip.appendChild(add);
    chip.appendChild(makeDeleteBtn("url", u.host, () => {
      URLS = URLS.filter(x => x.host !== u.host);
      renderUrls();
    }));

    chip.addEventListener("dragstart", (e) => {
      draggingHost = u.host;
      chip.classList.add("dragging");
      document.body.classList.add("dragging-url");
      e.dataTransfer.effectAllowed = "copy";
      e.dataTransfer.setData("text/plain", u.host);
    });
    chip.addEventListener("dragend", () => {
      draggingHost = null;
      chip.classList.remove("dragging");
      document.body.classList.remove("dragging-url");
      document.querySelectorAll(".drop-hover").forEach(el => el.classList.remove("drop-hover"));
    });
    list.appendChild(chip);
  }
}

async function loadUrls() {
  try {
    const r = await fetch("/api/projects/visited-urls?days=" + urlDays);
    URLS = (await r.json()).urls || [];
  } catch (e) {
    URLS = [];
  }
  renderUrls();
}

document.getElementById("urlsDays").addEventListener("click", (e) => {
  const pill = e.target.closest(".day-pill");
  if (!pill) return;
  urlDays = parseInt(pill.dataset.days, 10);
  document.querySelectorAll("#urlsDays .day-pill").forEach(p => p.classList.toggle("active", p === pill));
  document.getElementById("urlsList").innerHTML = '<span class="hint" style="margin:0">Lade…</span>';
  loadUrls();
});
document.getElementById("showAssigned").addEventListener("change", renderUrls);

// ---- Erkannte Topics: Laden, Rendern, Drag & Drop -------------------------
function renderTopics() {
  const list = document.getElementById("topicsList");
  const showAssigned = document.getElementById("showAssignedTopics").checked;
  list.innerHTML = "";
  const visible = TOPICS.filter(t => showAssigned || !t.project);
  if (!visible.length) {
    const s = document.createElement("span");
    s.className = "hint";
    s.style.margin = "0";
    s.textContent = TOPICS.length
      ? "Alle erkannten Topics sind bereits einem Projekt zugeordnet."
      : "Keine Topics im Zeitraum gefunden. Topics werden vom lokalen LLM beim Aggregieren erzeugt.";
    list.appendChild(s);
    return;
  }
  for (const t of visible) {
    const chip = document.createElement("span");
    chip.className = "url-chip topic-chip" + (t.project ? " assigned" : "");
    chip.draggable = true;
    const tparts = ["≈ " + (t.minutes < 1 ? "<1" : t.minutes) + " min"];
    if (t.topic_long) tparts.push(t.topic_long);
    if (t.project) tparts.push("zugeordnet: " + t.project);
    else if (t.observed_project) tparts.push("zuletzt meist: " + t.observed_project);
    chip.title = tparts.join("\n");

    const name = document.createElement("span");
    name.className = "u-host";
    name.textContent = t.topic;
    chip.appendChild(name);
    const cnt = document.createElement("span");
    cnt.className = "u-count";
    cnt.textContent = (t.minutes < 1 ? "<1" : t.minutes) + "m";
    chip.appendChild(cnt);
    if (t.project) {
      const pr = document.createElement("span");
      pr.className = "u-proj clickable";
      pr.textContent = "→ " + t.project;
      pr.title = "Topic „" + t.topic + "“ erneut „" + t.project + "“ zuweisen";
      pr.draggable = false;
      pr.addEventListener("click", (e) => { e.stopPropagation(); addTopicToProject(t.project, t.topic); });
      chip.appendChild(pr);
    } else if (t.observed_project) {
      const ob = document.createElement("span");
      ob.className = "u-obs clickable";
      ob.textContent = "≈ " + t.observed_project;
      ob.title = "Topic „" + t.topic + "“ dem Projekt „" + t.observed_project + "“ zuweisen";
      ob.draggable = false;
      ob.addEventListener("click", (e) => { e.stopPropagation(); addTopicToProject(t.observed_project, t.topic); });
      chip.appendChild(ob);
    }
    if (t.project_category) chip.appendChild(catBadge(t.project_category, "proj"));
    // Pool-Kategorie (Projekt → Web → Tool-Brücke) statt roher App-Kategorie
    else if (t.category) chip.appendChild(catBadge(t.category, "web"));
    else if (t.app_category) chip.appendChild(catBadge(t.app_category, "web"));
    chip.appendChild(makeDeleteBtn("topic", t.topic, () => {
      TOPICS = TOPICS.filter(x => x.topic !== t.topic);
      renderTopics();
    }));

    chip.addEventListener("dragstart", (e) => {
      draggingTopic = t.topic;
      chip.classList.add("dragging");
      document.body.classList.add("dragging-topic");
      e.dataTransfer.effectAllowed = "copy";
      e.dataTransfer.setData("text/plain", t.topic);
    });
    chip.addEventListener("dragend", () => {
      draggingTopic = null;
      chip.classList.remove("dragging");
      document.body.classList.remove("dragging-topic");
      document.querySelectorAll(".drop-hover").forEach(el => el.classList.remove("drop-hover"));
    });
    list.appendChild(chip);
  }
}

async function loadTopics() {
  try {
    const r = await fetch("/api/projects/recognized-topics?days=" + topicDays);
    TOPICS = (await r.json()).topics || [];
  } catch (e) {
    TOPICS = [];
  }
  renderTopics();
}

document.getElementById("topicsDays").addEventListener("click", (e) => {
  const pill = e.target.closest(".day-pill");
  if (!pill) return;
  topicDays = parseInt(pill.dataset.days, 10);
  document.querySelectorAll("#topicsDays .day-pill").forEach(p => p.classList.toggle("active", p === pill));
  document.getElementById("topicsList").innerHTML = '<span class="hint" style="margin:0">Lade…</span>';
  loadTopics();
});
document.getElementById("showAssignedTopics").addEventListener("change", renderTopics);

const btnNew = document.getElementById("btnNew");
btnNew.addEventListener("click", () => openEditor(""));
// „+ Neues Projekt“ ist auch Drop-Ziel für URL-Chips
btnNew.addEventListener("dragover", (e) => {
  if (!draggingHost) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
  btnNew.classList.add("drop-hover");
});
btnNew.addEventListener("dragleave", () => btnNew.classList.remove("drop-hover"));
btnNew.addEventListener("drop", (e) => {
  if (!draggingHost) return;
  e.preventDefault();
  btnNew.classList.remove("drop-hover");
  newProjectFromHost(draggingHost);
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && openName !== null) closeEditor(false);
});
document.getElementById("btnRestartAgg").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  const lab = btn.querySelector(".btn-label");
  const orig = lab.textContent;
  btn.disabled = true;
  lab.textContent = "…restarting";
  try {
    await postJSON("/api/config/restart/aggregator", {});
    toast("Aggregator neu gestartet", "ok");
  } catch (err) {
    toast("Restart fehlgeschlagen: " + err.message, "err");
  } finally {
    btn.disabled = false;
    lab.textContent = orig;
  }
});

async function load() {
  let data;
  try {
    const r = await fetch("/api/projects");
    data = await r.json();
  } catch (e) {
    document.getElementById("groups").innerHTML =
      '<div class="hint" style="color:var(--err)">Fehler beim Laden: ' + e.message + "</div>";
    return;
  }
  DB = data;
  document.getElementById("patPath").textContent = data.patterns_path || "";
  render();
}

// Pool-Farben/-Aliase zuerst laden, damit der erste Render sie nutzt
loadCatPool().finally(() => {
  load();
  loadUrls();
  loadTopics();
});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Einheitliche Navbar — eine Quelle, in alle Templates injiziert
# ---------------------------------------------------------------------------
# Read the WorkTracker version once. Single source of truth is the shipped
# config.default.yaml (bumped per release); the user config.yaml is only a
# fallback, since it is bootstrapped once and may carry a stale version.
def _wt_version() -> str:
    for f in (CONFIG_DEFAULT_FILE, CONFIG_FILE):
        try:
            v = (yaml.safe_load(f.read_text()) or {}).get("version")
            if v:
                return "v" + str(v)
        except Exception:
            pass
    return ""


WT_VERSION = _wt_version()

# Self-contained colours mirroring docs/index.html (acid-green primary, red
# colon, slate badges) so the dashboard navbar matches the documentation skin
# on every page — including the differently-themed config page.
# Light-Mode: zentrale Variablen-Overrides für ALLE Seiten-Paletten
# (Dashboard/Explore/Stats/Screenshots nutzen --bg/--fg/…, Config/Projects
# nutzen --panel/--muted/… — beide Sets werden hier überschrieben).
NAV_CSS = """<style>
:root{color-scheme:dark;}
html[data-theme="light"]{
  color-scheme:light;
  --bg:#eef1f4; --bg2:#ffffff; --bg3:#e3e9ee;
  --fg:#27313c; --fg2:#5d6b7a; --fg3:#9aa6b2;
  --cyan:#0e8da6; --green:#1a9e4b; --yellow:#9a7b00; --acid:#84a300;
  --red:#d4383b; --purple:#7d56d4; --blue:#0e8da6; --orange:#c06a10;
  --border:#d5dde4; --white:#10161c;
  --card-top:#ffffff; --card-hover:#b9c5d0;
  --panel:#ffffff; --panel2:#e9edf2; --muted:#5d6b7a;
  --accent:#84a300; --accent2:#0e8da6; --ok:#1a9e4b; --warn:#c06a10;
  --err:#d4383b; --violet:#7d56d4;
}
html{scrollbar-color:#3a4754 transparent;}
html[data-theme="light"]{scrollbar-color:#b9c5d0 transparent;}
body{transition:background .2s ease,color .2s ease;}
.wt-nav{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  position:sticky;top:10px;z-index:80;
  background:rgba(20,26,32,.88);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
  border:1px solid #2b3642;border-radius:12px;
  padding:9px 16px;margin:0 0 18px;
  box-shadow:0 8px 24px -18px rgba(0,0,0,.8);
  font-family:'SF Mono','Fira Code','JetBrains Mono',ui-monospace,monospace;}
html[data-theme="light"] .wt-nav{background:rgba(255,255,255,.85);
  border-color:#d5dde4;box-shadow:0 8px 24px -18px rgba(30,45,60,.45);}
.wt-nav .wt-brand{display:flex;align-items:center;gap:6px;
  font-size:18px;font-weight:200;letter-spacing:1px;color:#d4f500;
  text-decoration:none;white-space:nowrap;font-family:inherit;}
html[data-theme="light"] .wt-nav .wt-brand{color:#5e7400;}
.wt-nav .wt-brand .colon{color:#ff4d4f;font-weight:400;animation:colonPulse 1s ease-in-out infinite;}
@keyframes colonPulse{0%,100%{opacity:.35;}50%{opacity:1;}}
.wt-nav .wt-brand .ver{font-size:11px;font-weight:400;letter-spacing:.3px;
  background:darkslategrey;color:#d4f500;padding:2px 7px 1px;opacity:.86;
  border:1px solid darkslategrey;margin-left:2px;}
html[data-theme="light"] .wt-nav .wt-brand .ver{background:#e3e9ee;color:#5e7400;border-color:#d5dde4;}
.wt-nav .wt-links{display:flex;gap:4px;flex-wrap:wrap;}
.wt-nav a.wt-link{color:#8a95a3;text-decoration:none;font-size:13px;
  padding:6px 12px;border-radius:7px;transition:background .12s,color .12s;white-space:nowrap;}
.wt-nav a.wt-link:hover{color:#f4f8ff;background:#1b222a;}
.wt-nav a.wt-link.active{color:#0c1116;background:#d4f500;font-weight:600;}
html[data-theme="light"] .wt-nav a.wt-link{color:#5d6b7a;}
html[data-theme="light"] .wt-nav a.wt-link:hover{color:#10161c;background:#e3e9ee;}
html[data-theme="light"] .wt-nav a.wt-link.active{color:#1c2400;background:#cde845;}
.wt-nav .wt-right{margin-left:auto;color:#8a95a3;font-size:12px;
  display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.wt-nav .wt-right .dot{color:#6fe28a;font-size:16px;vertical-align:middle;
  display:inline-block;transition:opacity .3s;}
/* Impulsanimation des grünen Kreises bei jeder Aktualisierung */
@keyframes wtDotPulse{
  0%{transform:scale(1);text-shadow:0 0 0 rgba(111,226,138,0);}
  28%{transform:scale(1.75);text-shadow:0 0 11px rgba(111,226,138,.95);}
  100%{transform:scale(1);text-shadow:0 0 0 rgba(111,226,138,0);}}
.wt-nav .wt-right .dot.pulsing{animation:wtDotPulse .65s ease-out;}
/* Countdown-Balken über der Uhr — zeigt Zeit bis zur nächsten Aktualisierung */
.wt-nav .wt-right .clock-wrap{display:inline-flex;flex-direction:column;
  align-items:stretch;gap:3px;line-height:1;}
.wt-nav .wt-right .rf-countdown{height:2px;min-width:56px;width:100%;
  background:rgba(138,149,163,.22);border-radius:2px;overflow:hidden;}
.wt-nav .wt-right .rf-countdown-bar{display:block;height:100%;width:100%;
  background:#6fe28a;border-radius:2px;transform-origin:left center;transform:scaleX(1);}
html[data-theme="light"] .wt-nav .wt-right .rf-countdown-bar{background:#3fae5a;}
/* Theme-Toggle (Sonne/Mond) */
.wt-theme-btn{display:inline-flex;align-items:center;justify-content:center;
  width:30px;height:30px;flex-shrink:0;cursor:pointer;border-radius:8px;
  background:transparent;border:1px solid #2b3642;color:#8a95a3;padding:0;
  transition:color .12s,border-color .12s,background .12s;}
.wt-theme-btn:hover{color:#d4f500;border-color:#d4f500;}
html[data-theme="light"] .wt-theme-btn{border-color:#d5dde4;color:#5d6b7a;}
html[data-theme="light"] .wt-theme-btn:hover{color:#9a7b00;border-color:#9a7b00;}
/* Icon zeigt den Modus, in den gewechselt wird: dunkel → Sonne, hell → Mond */
.wt-theme-btn .ico-sun{display:block;}
.wt-theme-btn .ico-moon{display:none;}
html[data-theme="light"] .wt-theme-btn .ico-sun{display:none;}
html[data-theme="light"] .wt-theme-btn .ico-moon{display:block;}
/* Gemeinsame Ladeanimation — auf jeder Seite verfügbar, da mit der Navbar injiziert.
   Deutlich auffälliger: großer Dual-Ring mit Glow + pulsierendem Label. */
.wt-loading{display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:16px;padding:48px 0;color:#8a95a3;font-size:13px;
  font-family:'SF Mono','Fira Code','JetBrains Mono',ui-monospace,monospace;
  animation:wtfade .25s ease;}
.wt-spinner{width:46px;height:46px;border-radius:50%;flex-shrink:0;position:relative;
  border:4px solid rgba(138,149,163,.18);
  border-top-color:#d4f500;border-right-color:#4fc3d8;
  box-shadow:0 0 22px rgba(212,245,0,.18),inset 0 0 12px rgba(79,195,216,.08);
  animation:wtspin .75s cubic-bezier(.55,.15,.45,.85) infinite;}
html[data-theme="light"] .wt-spinner{border-top-color:#84a300;border-right-color:#0e8da6;
  box-shadow:0 0 22px rgba(132,163,0,.25),inset 0 0 12px rgba(14,141,166,.08);}
.wt-spinner.sm{width:16px;height:16px;border-width:2px;display:inline-block;
  vertical-align:-3px;margin-right:8px;box-shadow:none;}
@keyframes wtspin{to{transform:rotate(360deg)}}
@keyframes wtfade{from{opacity:0}to{opacity:1}}
.wt-load-label{animation:wtpulse 1.2s ease-in-out infinite;letter-spacing:.6px;
  font-size:13px;text-transform:uppercase;}
@keyframes wtpulse{0%,100%{opacity:.4}50%{opacity:1}}
/* Inline-Variante (Spinner + Label nebeneinander, z. B. in Toolbars) */
.wt-loading.inline{flex-direction:row;padding:0;gap:10px;animation:none;}
.wt-loading.inline .wt-load-label{text-transform:none;}
</style>"""

# Theme-Bootstrap: läuft VOR dem Navbar-Markup, damit der gespeicherte Mode
# ohne Flackern angewendet wird. Stellt wtToggleTheme() global bereit und
# feuert 'wt-theme', damit Seiten (z. B. ECharts auf /statistics) neu rendern.
NAV_JS = """<script>
(function(){
  var saved = null;
  try { saved = localStorage.getItem('wt_theme'); } catch(e) {}
  document.documentElement.setAttribute('data-theme', saved === 'light' ? 'light' : 'dark');
  window.wtToggleTheme = function(){
    var next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('wt_theme', next); } catch(e) {}
    window.dispatchEvent(new CustomEvent('wt-theme', {detail: next}));
  };
})();
</script>"""

_NAV_ITEMS = [
    ("dashboard", "/", "Dashboard"),
    ("explore", "/explore", "Explore"),
    ("statistics", "/statistics", "Statistics"),
    ("screenshots", "/screenshots", "Screenshots"),
    ("projects", "/projects", "Projects/Topics"),
    ("config", "/config", "Config"),
]


# Sonne/Mond-Icons für den Theme-Toggle (Feather-Style, currentColor)
_THEME_BTN = (
    '<button class="wt-theme-btn" type="button" onclick="wtToggleTheme()"'
    ' title="Light/Dark Mode umschalten" aria-label="Theme umschalten">'
    '<svg class="ico-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15">'
    '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
    '<svg class="ico-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="15" height="15">'
    '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/>'
    '<line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>'
    '<line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/>'
    '<line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>'
    '<line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>'
    '</button>'
)


def _navbar(active, right=""):
    links = "".join(
        '<a class="wt-link{cls}" href="{href}">{label}</a>'.format(
            cls=" active" if key == active else "", href=href, label=label
        )
        for key, href, label in _NAV_ITEMS
    )
    ver = ('<span class="ver">' + WT_VERSION + "</span>") if WT_VERSION else ""
    brand = (
        '<a class="wt-brand" href="/">'
        'WORK<span class="colon">:</span>TRACKER' + ver + "</a>"
    )
    return (
        NAV_JS
        + NAV_CSS
        + '<nav class="wt-nav">'
        + brand
        + '<div class="wt-links">' + links + "</div>"
        + '<div class="wt-right">' + right + "</div>"
        + _THEME_BTN
        + "</nav>"
    )


# Per-Seiten-Inhalt der rechten Navbar-Seite (vorher in den einzelnen Headern)
_NAV_RIGHT = {
    "dashboard": (
        '<span class="dot" id="pulse">●</span>'
        '<span class="clock-wrap">'
        '<span class="rf-countdown"><span class="rf-countdown-bar" id="rf-bar"></span></span>'
        '<span id="clock">—</span>'
        '</span>'
        "<span>·</span>"
        '<span><span id="snap-count">—</span> Snapshots</span>'
    ),
    "explore": '<span id="status-text"></span>',
    "statistics": '<span id="clock">—</span>',
    "screenshots": (
        '<div class="dp" id="dp">'
        '<button class="dp-trigger" id="dp-trigger" type="button">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">'
        '<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/>'
        '<line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>'
        '<span id="dp-label">—</span></button>'
        '<div class="dp-pop" id="dp-pop" hidden>'
        '<div class="dp-head">'
        '<button class="dp-nav" id="dp-prev" type="button">&lsaquo;</button>'
        '<span class="dp-title" id="dp-title"></span>'
        '<button class="dp-nav" id="dp-next" type="button">&rsaquo;</button>'
        '</div>'
        '<div class="dp-weekdays"><span>Mo</span><span>Di</span><span>Mi</span>'
        '<span>Do</span><span>Fr</span><span>Sa</span><span>So</span></div>'
        '<div class="dp-grid" id="dp-grid"></div>'
        '</div></div>'
        '<span class="summary" id="summary"></span>'
    ),
    "projects": '<span><span id="proj-count">—</span> Projekte</span>',
    # The config toolbar (path + open/restart buttons) lives in its own bar
    # BELOW the navbar (see @@CFG_TOOLBAR@@ in CONFIG_HTML), so the navbar
    # stays clean and consistent with the other pages.
    "config": "",
}

# Standalone toolbar rendered directly under the navbar on the config page.
# Same element ids as before so the existing JS handlers keep working.
CONFIG_TOOLBAR = (
    '<div class="cfg-toolbar">'
    '<span class="path" id="cfgPath"></span>'
    '<div class="cfg-toolbar-actions">'
    '<button id="btnOpenDefault" title="config.default.yaml im Editor öffnen">'
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'
    '<span class="btn-label">Open config.default.yaml</span></button>'
    '<button id="btnOpenUser" title="config.yaml im Editor öffnen">'
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'
    '<span class="btn-label">Open config.yaml</span></button>'
    '<button id="btnRestartCol" class="primary">'
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>'
    '<span class="btn-label">Restart Collector</span></button>'
    '<button id="btnRestartAgg" class="primary">'
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>'
    '<span class="btn-label">Restart Aggregator</span></button>'
    '</div>'
    '</div>'
)


def _inject_navbar(html, page):
    return html.replace("@@NAV:%s@@" % page, _navbar(page, _NAV_RIGHT[page]))


HTML = _inject_navbar(HTML, "dashboard")
EXPLORE_HTML = _inject_navbar(EXPLORE_HTML, "explore")
STATS_HTML = _inject_navbar(STATS_HTML, "statistics")
SCREENSHOTS_HTML = _inject_navbar(SCREENSHOTS_HTML, "screenshots")
PROJECTS_HTML = _inject_navbar(PROJECTS_HTML, "projects")
CONFIG_HTML = _inject_navbar(CONFIG_HTML, "config")
CONFIG_HTML = CONFIG_HTML.replace("@@CFG_TOOLBAR@@", CONFIG_TOOLBAR)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7880"))
    app.run(host="127.0.0.1", port=port, debug=False)
