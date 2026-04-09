#!/usr/bin/env python3
"""WorkTracker Web Dashboard — Flask Server"""

import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
import subprocess

from web_categories import build_web_category_tree

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
DATA_SNAP = BASE / "data" / "snapshots"
DATA_SESS = BASE / "data" / "sessions"
SUMMARIES = BASE / "summaries"
LOGS = BASE / "logs"
PATTERNS_FILE = BASE / "daemon" / "project_patterns.yaml"

# Apps that represent inactive/lock-screen state — excluded from stats
INACTIVE_APPS = {"loginwindow"}


def _load_app_categories():
    """Load app_categories from project_patterns.yaml."""
    try:
        with open(PATTERNS_FILE) as f:
            data = yaml.safe_load(f) or {}
        return data.get("app_categories", {})
    except Exception:
        return {}


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
    name_lower = _clean_name(app_name).lower()
    for cat, patterns in categories.items():
        if cat == "Other":
            continue
        for pat in patterns:
            if fnmatch.fnmatch(name_lower, pat.lower()):
                return cat
    return "Other"


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
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def snapshot_count(date_str):
    path = DATA_SNAP / f"{date_str}.jsonl"
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def launchd_status(label):
    try:
        r = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return {"loaded": False}
        info = {"loaded": True, "pid": None, "exit": None}
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
        return info
    except Exception:
        return {"loaded": False}


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

    # Load config for interval
    try:
        import yaml as _yaml
        with open(BASE / "daemon" / "config.yaml") as _cf:
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
            "projects": proj_list,
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
        recent_sess.append({
            "time": t,
            "app": s.get("app_name", "—"),
            "title": (s.get("window_title") or "—")[:60],
            "project": s.get("project", ""),
            "dur": s.get("duration_seconds", 0),
            "intensity": s.get("intensity_score", 0),
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
    """Return heatmap data for the last N weeks."""
    from rhythm_heatmap import get_active_hours, HEALTHY_START, HEALTHY_END
    from datetime import timedelta as td

    weeks = min(weeks, 8)
    today = datetime.now()
    days = weeks * 7
    result = []

    for i in range(days - 1, -1, -1):
        day = today - td(days=i)
        ds = day.strftime("%Y-%m-%d")
        filepath = SUMMARIES / "daily" / f"{ds}.md"
        hours = get_active_hours(filepath)
        cells = []
        for h in range(24):
            if h in hours:
                cells.append("healthy" if HEALTHY_START <= h < HEALTHY_END else "unhealthy")
            else:
                cells.append("missed" if HEALTHY_START <= h < HEALTHY_END else "rest")
        result.append({
            "date": ds,
            "weekday": day.strftime("%a"),
            "weekend": day.weekday() >= 5,
            "today": i == 0,
            "hours": cells,
            "active": len(hours),
            "healthy": sum(1 for h in hours if HEALTHY_START <= h < HEALTHY_END),
            "unhealthy": sum(1 for h in hours if h < HEALTHY_START or h >= HEALTHY_END),
        })

    total_active = sum(d["active"] for d in result if d["active"])
    total_healthy = sum(d["healthy"] for d in result)
    days_tracked = sum(1 for d in result if d["active"])

    return jsonify({
        "days": result,
        "healthy_start": HEALTHY_START,
        "healthy_end": HEALTHY_END,
        "stats": {
            "avg_active": round(total_active / days_tracked, 1) if days_tracked else 0,
            "healthy_pct": round(total_healthy / total_active * 100) if total_active else 0,
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


# ── HTML ─────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkTracker Dashboard</title>
<style>
:root {
  --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
  --fg: #e6edf3; --fg2: #8b949e; --fg3: #484f58;
  --cyan: #58a6ff; --green: #3fb950; --yellow: #d29922;
  --red: #f85149; --purple: #bc8cff; --blue: #388bfd;
  --orange: #d18616;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  background: var(--bg); color: var(--fg);
  font-size: 13px; line-height: 1.5;
  padding: 12px; max-width: 1400px; margin: 0 auto;
}
h1 { font-size: 18px; color: var(--cyan); font-weight: 600; }
h2 {
  font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px;
  color: var(--fg3); margin-bottom: 8px; padding-bottom: 4px;
  border-bottom: 1px solid var(--bg3);
}
.header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 0 12px; border-bottom: 1px solid var(--bg3); margin-bottom: 16px;
}
.header-right { color: var(--fg2); font-size: 12px; }
.header-right .dot { color: var(--green); font-size: 16px; vertical-align: middle; }
.grid {
  display: grid; gap: 12px;
  grid-template-columns: 1fr 1fr;
}
@media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
.card {
  background: var(--bg2); border: 1px solid var(--bg3);
  border-radius: 8px; padding: 14px;
}
.card.wide { grid-column: 1 / -1; }
.pill {
  display: inline-block; padding: 2px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 600;
}
.pill.ok { background: #0d3321; color: var(--green); }
.pill.warn { background: #3d2e00; color: var(--yellow); }
.pill.err { background: #3d1418; color: var(--red); }
.pill.idle { background: #1c2333; color: var(--blue); }

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
.svc { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 12px; }
.svc-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.svc-dot.on { background: var(--green); box-shadow: 0 0 6px var(--green); }
.svc-dot.sched { background: var(--blue); }
.svc-dot.off { background: var(--fg3); }
.svc-name { color: var(--fg); font-weight: 500; min-width: 110px; }
.svc-info { color: var(--fg2); }

/* Stats */
.stats { display: flex; flex-wrap: wrap; gap: 6px 20px; }
.stat { text-align: center; min-width: 80px; }
.stat-val { font-size: 22px; font-weight: 700; color: var(--fg); }
.stat-label { font-size: 10px; color: var(--fg2); text-transform: uppercase; }

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
.sess-dur-wrap { flex: 0.6; display: flex; align-items: center; gap: 6px; min-width: 100px; }
.sess-dur { flex-shrink: 0; text-align: right; color: var(--yellow); white-space: nowrap; }
.sess-dur-bar { flex: 1; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.sess-dur-fill { height: 100%; border-radius: 3px; background: #888; }
.sess-int-wrap { width: 36px; flex-shrink: 0; display: flex; align-items: flex-end; gap: 2px; height: 14px; }
.sess-int-seg { width: 4px; border-radius: 1px; }

/* Chart */
.chart-wrap { height: 120px; display: flex; align-items: flex-end; gap: 2px; padding-top: 8px; }
.chart-bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; }
.chart-bar {
  width: 100%; min-width: 8px; background: var(--cyan); border-radius: 3px 3px 0 0;
  transition: height 0.5s; opacity: 0.7;
}
.chart-bar.now { opacity: 1; background: var(--green); }
.chart-lbl { font-size: 9px; color: var(--fg3); margin-top: 2px; }

/* Reports */
.rpt { display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 12px; }
.rpt-type { width: 65px; color: var(--fg2); font-weight: 500; }
.rpt-name { color: var(--cyan); cursor: pointer; text-decoration: none; }
.rpt-name:hover { text-decoration: underline; }
.rpt-size { color: var(--fg3); font-size: 11px; }
.rpt-age { color: var(--fg3); font-size: 11px; }
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
.log-line { font-size: 11px; color: var(--fg3); padding: 1px 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

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
.heatmap-cell.rest { background: transparent; }
.heatmap-cell.missed { background: var(--bg3); }
.heatmap-cell.healthy { background: var(--green); opacity: 0.85; }
.heatmap-cell.unhealthy { background: var(--red); opacity: 0.75; }
.heatmap-cell:hover { opacity: 1; outline: 1px solid var(--fg2); }
.heatmap-hours { display: flex; margin-left: 78px; margin-bottom: 4px; }
.heatmap-hours span { width: 13px; font-size: 8px; color: var(--fg3); text-align: center; }
.heatmap-legend {
  display: flex; gap: 16px; margin-top: 8px; margin-left: 78px; font-size: 11px; color: var(--fg2);
}
.heatmap-legend-dot {
  display: inline-block; width: 10px; height: 10px; border-radius: 2px;
  vertical-align: middle; margin-right: 4px;
}
.heatmap-stats {
  display: flex; gap: 20px; margin-top: 10px; margin-left: 78px; font-size: 12px;
}
.heatmap-stats .stat-val { font-size: 18px; }
.heatmap-sep { border: none; border-top: 1px solid var(--bg3); margin: 2px 0 2px 78px; }
.heatmap-day-total {
  width: 30px; flex-shrink: 0; font-size: 10px; color: var(--fg3);
  text-align: right; padding-left: 4px;
}

/* Idle overlay */
.idle-banner {
  background: #1c2333; border: 1px solid var(--blue); border-radius: 6px;
  padding: 6px 12px; margin-top: 8px; color: var(--blue); font-size: 12px;
  display: none;
}
.idle-banner.show { display: block; }
</style>
</head>
<body>

<div class="header">
  <h1>WorkTracker &nbsp;<a href="/explore" style="font-size:12px;font-weight:400;color:var(--fg2)">Explore &rarr;</a></h1>
  <div class="header-right">
    <span class="dot" id="pulse">●</span>
    <span id="clock">—</span>
    &nbsp;·&nbsp;
    <span id="snap-count">—</span> Snapshots
  </div>
</div>

<div class="grid">

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

  <!-- Daily Overview -->
  <div class="card wide">
    <h2 id="day-title">Today</h2>
    <div class="stats" id="day-stats"></div>
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
    <div class="modal-body" id="modal-body">Loading...</div>
  </div>
</div>

<script>
const REFRESH = 3000;
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
  $('modal-body').textContent = 'Loading...';
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

function update(d) {
  // Clock
  $('clock').textContent = new Date(d.ts).toLocaleTimeString('en');
  $('snap-count').textContent = d.snap_total.toLocaleString('en');

  // Pulse
  const pulse = $('pulse');
  pulse.style.opacity = '1';
  setTimeout(() => pulse.style.opacity = '0.3', 500);

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

  // Services
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
    if (s && s.loaded) {
      if (s.pid) { dot = 'on'; info = 'PID ' + s.pid; }
      else { dot = 'sched'; info = sched + (s.exit != null ? '  exit=' + s.exit : ''); }
    }
    sh += '<div class="svc"><span class="svc-dot ' + dot + '"></span>'
        + '<span class="svc-name">' + name + '</span>'
        + '<span class="svc-info">' + info + '</span></div>';
  }
  $('services').innerHTML = sh;

  // Day stats
  const dy = d.day;
  if (dy) {
    $('day-title').textContent = 'Today · ' + d.today;
    $('day-stats').innerHTML = [
      ['Active', fmt(dy.total_sec)],
      ['Sessions', dy.sessions],
      ['Focus', dy.focus_count + ' (' + fmt(dy.focus_sec) + ')'],
      ['Switches', dy.switches + ' (' + dy.switches_ph + '/h)'],
      ['Keys', dy.keys.toLocaleString('en')],
      ['Clicks', dy.clicks.toLocaleString('en')],
      ['Scroll', dy.scrolls.toLocaleString('en')],
      ['Clipboard', dy.clipboard + 'x'],
    ].map(([l, v]) => '<div class="stat"><div class="stat-val">' + v + '</div><div class="stat-label">' + l + '</div></div>').join('');

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

    // Hourly chart
    const maxH = Math.max(...dy.hourly, 1);
    const nowH = new Date().getHours();
    let ch = '';
    for (let h = 6; h < 24; h++) {
      const pct = dy.hourly[h] / maxH * 100;
      const cls = h === nowH ? ' now' : '';
      ch += '<div class="chart-bar-wrap">'
          + '<div class="chart-bar' + cls + '" style="height:' + pct + '%"></div>'
          + '<div class="chart-lbl">' + h + '</div>'
          + '</div>';
    }
    $('hourly-chart').innerHTML = ch;
  } else {
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
    return '<div class="sess-row">'
        + '<div class="sess-time">' + s.time + '</div>'
        + '<div class="sess-app">' + esc(s.app) + '</div>'
        + '<div class="sess-title">' + esc(s.title) + '</div>'
        + '<div class="sess-proj">' + esc(s.project) + '</div>'
        + '<div class="sess-dur-wrap"><div class="sess-dur">' + fmt(s.dur) + '</div>'
        + '<div class="sess-dur-bar"><div class="sess-dur-fill" style="width:' + durPct + '%"></div></div></div>'
        + '<div class="sess-int-wrap">' + bars + '</div>'
        + '</div>';
  });
  $('sessions').innerHTML = sessHtml || '<div style="color:var(--fg2)">—</div>';

  // Reports
  let rh = '';
  for (const [type, label] of [['daily', 'Daily'], ['weekly', 'Weekly'], ['monthly', 'Monthly']]) {
    const g = d.report_groups ? d.report_groups[type] : null;
    rh += '<div style="margin-bottom:10px">';
    rh += '<div style="font-size:11px;color:var(--fg2);font-weight:600;margin-bottom:4px">' + label + '</div>';
    if (g) {
      for (const [key, icon] of [['short', '◆'], ['summary', '●'], ['raw', '○']]) {
        const f = g[key];
        if (f) {
          const kb = (f.size / 1024).toFixed(1);
          rh += '<div class="rpt">'
              + '<span style="color:var(--fg3);width:16px;text-align:center;font-size:10px">' + icon + '</span>'
              + '<span class="rpt-name" onclick="openFile(\'' + type + '\',\'' + esc(f.name) + '\')" title="Open in editor">' + esc(f.name) + '</span>'
              + '<span class="rpt-size">(' + kb + ' KB)</span>'
              + '<span class="rpt-age">' + fmtAge(f.mtime) + '</span>'
              + '<span class="rpt-view" onclick="openReport(\'' + type + '\',\'' + esc(f.name) + '\')" title="Preview" style="cursor:pointer;color:var(--fg3);font-size:11px;margin-left:auto">👁</span>'
              + '</div>';
        }
      }
    } else {
      rh += '<div style="color:var(--fg3);font-size:12px;padding:2px 0">—</div>';
    }
    rh += '</div>';
  }
  $('reports').innerHTML = rh;

  // Logs
  $('logs').innerHTML = (d.logs || []).map(l =>
    '<div class="log-line">' + esc(l) + '</div>'
  ).join('');
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

  // Hour labels
  html += '<div class="heatmap-hours">';
  for (let h = 0; h < 24; h++) {
    html += '<span>' + (h % 3 === 0 ? h : '') + '</span>';
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
    d.hours.forEach((c, h) => {
      html += '<div class="heatmap-cell ' + c + '" title="' + d.date + ' ' + h + ':00 — ' + c + '"></div>';
    });
    html += '<div class="heatmap-day-total">' + (d.active > 0 ? d.active + 'h' : '') + '</div>';
    html += '</div>';
  });

  // Legend
  html += '<div class="heatmap-legend">';
  html += '<span><span class="heatmap-legend-dot" style="background:var(--green)"></span>Active (good)</span>';
  html += '<span><span class="heatmap-legend-dot" style="background:var(--red)"></span>Active (late/early)</span>';
  html += '<span><span class="heatmap-legend-dot" style="background:var(--bg3)"></span>Missed core time</span>';
  html += '</div>';

  // Stats
  const s = data.stats;
  html += '<div class="heatmap-stats">';
  html += '<div class="stat"><div class="stat-val">' + s.avg_active + 'h</div><div class="stat-label">Avg/Day</div></div>';
  html += '<div class="stat"><div class="stat-val" style="color:var(--green)">' + s.healthy_pct + '%</div><div class="stat-label">Healthy</div></div>';
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
  --bg: #0d1117; --bg2: #161b22; --bg3: #21262d;
  --fg: #e6edf3; --fg2: #8b949e; --fg3: #484f58;
  --cyan: #58a6ff; --green: #3fb950; --yellow: #d29922;
  --red: #f85149; --purple: #bc8cff; --blue: #388bfd;
  --orange: #d18616;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
  background: var(--bg); color: var(--fg);
  font-size: 13px; line-height: 1.5;
  padding: 12px; max-width: 1400px; margin: 0 auto;
}
a { color: var(--cyan); text-decoration: none; }
a:hover { text-decoration: underline; }
h2 {
  font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px;
  color: var(--fg3); margin-bottom: 8px; padding-bottom: 4px;
  border-bottom: 1px solid var(--bg3);
}
.card {
  background: var(--bg2); border: 1px solid var(--bg3);
  border-radius: 8px; padding: 14px; margin-bottom: 12px;
}

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
.date-label {
  font-size: 16px; font-weight: 700; color: var(--fg); cursor: pointer;
  padding: 4px 8px; border-radius: 6px;
}
.date-label:hover { background: var(--bg3); }
.date-input {
  position: absolute; opacity: 0; width: 0; height: 0;
}

/* Stats */
.stats { display: flex; flex-wrap: wrap; gap: 6px 20px; }
.stat { text-align: center; min-width: 80px; }
.stat-val { font-size: 22px; font-weight: 700; color: var(--fg); }
.stat-label { font-size: 10px; color: var(--fg2); text-transform: uppercase; }

/* Distribution grid */
.dist-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
@media (max-width: 800px) { .dist-grid { grid-template-columns: 1fr; } }
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
.sess-dur-wrap { flex: 0.6; display: flex; align-items: center; gap: 6px; min-width: 100px; }
.sess-dur { flex-shrink: 0; text-align: right; color: var(--yellow); white-space: nowrap; }
.sess-dur-bar { flex: 1; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.sess-dur-fill { height: 100%; border-radius: 3px; background: #888; }
.sess-int-wrap { width: 36px; flex-shrink: 0; display: flex; align-items: flex-end; gap: 2px; height: 14px; }
.sess-int-seg { width: 4px; border-radius: 1px; }
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

<div class="header">
  <h1><a href="/" style="color:var(--fg2);font-size:12px">Dashboard</a> &nbsp;/ &nbsp;Explore</h1>
  <div class="header-right" id="status-text"></div>
</div>

<!-- Date Nav -->
<div class="date-nav">
  <button onclick="prevDay()" id="btn-prev">&larr;</button>
  <span class="date-label" id="date-label" onclick="document.getElementById('date-pick').showPicker()">—</span>
  <input type="date" id="date-pick" class="date-input" onchange="goDate(this.value)">
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
    <input type="text" id="f-text" placeholder="Suche..." oninput="applyFilters()">
    <select id="f-app" onchange="applyFilters()"><option value="">Alle Apps</option></select>
    <select id="f-proj" onchange="applyFilters()"><option value="">Alle Projekte</option></select>
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
  document.getElementById('date-pick').value = currentDate;
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
function goDate(val) { if (availableDates.includes(val)) loadDate(val); }

async function loadDate(date) {
  currentDate = date;
  expandedIdx = null;
  loadedSnapshots = {};
  colorIdx = 0;
  Object.keys(appColorMap).forEach(k => delete appColorMap[k]);

  updateDateUI();
  document.getElementById('sessions-list').innerHTML = '<div class="loading">Laden...</div>';

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

  document.getElementById('day-stats').innerHTML = [
    ['Aktiv', fmt(totalSec)],
    ['Sessions', sessions.length],
    ['Focus', focus.length + ' (' + fmt(focusSec) + ')'],
    ['Switches', switches],
    ['Keys', keys.toLocaleString()],
    ['Clicks', clicks.toLocaleString()],
    ['Scroll', scrolls.toLocaleString()],
    ['Clipboard', clip + 'x'],
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

  // Store dist data globally for search
  window._distData = { Apps: appDist, Projekte: projDist };

  function distRowHtml(title, name, sec, max) {
    const pct = Math.round(sec / totalSec * 100);
    const c = title === 'Apps' ? appColor(name) : 'var(--cyan)';
    return '<div class="bar-row">'
      + '<span class="app-dot" style="background:'+c+'"></span>'
      + '<div class="bar-name">' + esc(name) + '</div>'
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

  document.getElementById('distributions').innerHTML =
    '<div class="dist-card">' + renderDist('Apps', appDist, '') + '</div>'
    + '<div class="dist-card">' + renderDist('Projekte', projDist, '') + '</div>';
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
    line.setAttribute('stroke', '#21262d'); line.setAttribute('stroke-width', '1');
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
  let ao = '<option value="">Alle Apps</option>';
  apps.forEach(a => ao += '<option>' + esc(a) + '</option>');
  document.getElementById('f-app').innerHTML = ao;
  let po = '<option value="">Alle Projekte</option>';
  projs.forEach(p => po += '<option>' + esc(p) + '</option>');
  document.getElementById('f-proj').innerHTML = po;
}

function applyFilters() {
  sessActivePage = 0;
  const text = document.getElementById('f-text').value.toLowerCase();
  const app = document.getElementById('f-app').value;
  const proj = document.getElementById('f-proj').value;

  filteredSessions = sessions.filter((s, i) => {
    s._origIdx = i;
    if (app && s.app_name !== app) return false;
    if (proj && (s.project || 'Other') !== proj) return false;
    if (text) {
      const hay = ((s.app_name || '') + ' ' + (s.window_title || '') + ' ' + (s.project || '')).toLowerCase();
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

function sessRowHtml(s) {
  const i = s._origIdx;
  const start = new Date(s.start);
  const t = pad(start.getHours()) + ':' + pad(start.getMinutes());
  const c = appColor(s.app_name);
  const expanded = expandedIdx === i;
  const durPct = Math.round((s.duration_seconds || 0) / _sessMaxDur * 100);
  const iv = Math.round(Math.min(s.intensity_score || 0, 10));
  let h = '<div class="sess-row' + (expanded ? ' expanded' : '') + '" id="sess-' + i + '" onclick="toggleSession(' + i + ')">'
    + '<span class="sess-arrow">\u25B6</span>'
    + '<div class="sess-time">' + t + '</div>'
    + '<span class="app-dot" style="background:'+c+'"></span>'
    + '<div class="sess-app">' + esc(s.app_name) + '</div>'
    + '<div class="sess-title">' + esc(s.window_title || '\u2014') + '</div>'
    + '<div class="sess-proj">' + esc(s.project || '') + '</div>'
    + '<div class="sess-dur-wrap"><div class="sess-dur">' + fmt(s.duration_seconds) + '</div>'
    + '<div class="sess-dur-bar"><div class="sess-dur-fill" style="width:'+durPct+'%"></div></div></div>'
    + '<div class="sess-int-wrap">' + intBars(s.intensity_score) + '</div>'
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
  if (s.url) h += '<div class="detail-field"><div class="detail-label">URL</div><div class="detail-val">' + esc(s.url) + '</div></div>';
  if (s.git_repo) h += '<div class="detail-field"><div class="detail-label">Git</div><div class="detail-val">' + esc(s.git_repo) + ' / ' + esc(s.git_branch || '\u2014') + '</div></div>';
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

  btn.textContent = 'Laden...';
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7880, debug=False)
