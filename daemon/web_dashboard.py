#!/usr/bin/env python3
"""WorkTracker Web Dashboard — Flask Server"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template_string
import subprocess

app = Flask(__name__)

BASE = Path.home() / "WorkTracker"
DATA_SNAP = BASE / "data" / "snapshots"
DATA_SESS = BASE / "data" / "sessions"
SUMMARIES = BASE / "summaries"
LOGS = BASE / "logs"


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
        span = len(recent) * 10
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

    # Daily statistics
    day_stats = None
    if sessions:
        total_sec = sum(s.get("duration_seconds", 0) for s in sessions)
        focus = [s for s in sessions if s.get("duration_seconds", 0) >= 1500]
        focus_sec = sum(s.get("duration_seconds", 0) for s in focus)
        apps = [s.get("app_name", "") for s in sessions]
        switches = sum(1 for i in range(1, len(apps)) if apps[i] != apps[i - 1])
        clip = sum(len(s.get("clipboard_events", [])) for s in sessions)
        keys = sum(s.get("keystrokes_total", 0) for s in sessions)
        clicks = sum(s.get("mouse_clicks_total", 0) for s in sessions)
        scrolls = sum(s.get("scroll_events_total", 0) for s in sessions)

        # Projects
        projects = {}
        for s in sessions:
            p = s.get("project", "Other")
            if p not in projects:
                projects[p] = {"sec": 0, "n": 0, "intensity": []}
            projects[p]["sec"] += s.get("duration_seconds", 0)
            projects[p]["n"] += 1
            i = s.get("intensity_score")
            if i is not None:
                projects[p]["intensity"].append(i)

        proj_list = []
        for pn, pd in sorted(projects.items(), key=lambda x: x[1]["sec"], reverse=True):
            avg_i = sum(pd["intensity"]) / len(pd["intensity"]) if pd["intensity"] else 0
            proj_list.append({
                "name": pn,
                "sec": pd["sec"],
                "pct": round(pd["sec"] / total_sec * 100, 1) if total_sec else 0,
                "sessions": pd["n"],
                "intensity": round(avg_i, 1),
            })

        # Apps
        app_times = {}
        for s in sessions:
            a = s.get("app_name", "Unknown")
            app_times[a] = app_times.get(a, 0) + s.get("duration_seconds", 0)
        app_list = [
            {"name": a, "sec": t, "pct": round(t / total_sec * 100, 1)}
            for a, t in sorted(app_times.items(), key=lambda x: x[1], reverse=True)[:10]
        ]

        # Hourly activity
        hourly = [0] * 24
        for s in sessions:
            try:
                h = datetime.fromisoformat(s["start"]).hour
                hourly[h] += s.get("duration_seconds", 0)
            except Exception:
                pass

        hrs = total_sec / 3600 if total_sec else 1

        day_stats = {
            "total_sec": total_sec,
            "sessions": len(sessions),
            "focus_count": len(focus),
            "focus_sec": focus_sec,
            "switches": switches,
            "switches_ph": round(switches / hrs, 1),
            "keys": keys,
            "clicks": clicks,
            "scrolls": scrolls,
            "clipboard": clip,
            "projects": proj_list,
            "apps": app_list,
            "hourly": hourly,
        }

    # Recent Sessions
    recent_sess = []
    for s in reversed(sessions[-12:]):
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
        "services": services,
        "live": live,
        "day": day_stats,
        "recent_sessions": recent_sess,
        "reports": reports,
        "report_groups": report_groups,
        "logs": logs,
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
.proj-name { width: 140px; flex-shrink: 0; color: var(--fg); font-weight: 500;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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
.sess-dur { width: 55px; flex-shrink: 0; text-align: right; color: var(--yellow); }
.sess-int-wrap { width: 50px; flex-shrink: 0; display: flex; align-items: center; gap: 4px; }
.sess-int-bar { width: 30px; height: 5px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.sess-int-fill { height: 100%; background: var(--green); border-radius: 3px; }

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
  <h1>WorkTracker</h1>
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
    ['collector', 'Collector', '10s interval'],
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

    // Projects
    let ph = '';
    dy.projects.forEach((p, i) => {
      const c = PROJ_COLORS[i % PROJ_COLORS.length];
      ph += '<div class="proj-row">'
          + '<div class="proj-name">' + esc(p.name) + '</div>'
          + '<div class="proj-bar-wrap"><div class="proj-bar" style="width:' + p.pct + '%;background:' + c + '"></div></div>'
          + '<div class="proj-pct">' + p.pct + '%</div>'
          + '<div class="proj-time">' + fmt(p.sec) + '</div>'
          + '<div class="proj-int">' + p.intensity + '</div>'
          + '</div>';
    });
    $('projects').innerHTML = ph;

    // Apps
    let ah = '';
    dy.apps.forEach((a, i) => {
      const c = PROJ_COLORS[i % PROJ_COLORS.length];
      ah += '<div class="proj-row">'
          + '<div class="proj-name">' + esc(a.name) + '</div>'
          + '<div class="proj-bar-wrap"><div class="proj-bar" style="width:' + a.pct + '%;background:' + c + '"></div></div>'
          + '<div class="proj-pct">' + a.pct + '%</div>'
          + '<div class="proj-time">' + fmt(a.sec) + '</div>'
          + '</div>';
    });
    $('apps').innerHTML = ah;

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
  let ss = '';
  (d.recent_sessions || []).forEach(s => {
    const ipct = Math.min(s.intensity / 10 * 100, 100);
    ss += '<div class="sess-row">'
        + '<div class="sess-time">' + s.time + '</div>'
        + '<div class="sess-app">' + esc(s.app) + '</div>'
        + '<div class="sess-title">' + esc(s.title) + '</div>'
        + '<div class="sess-proj">' + esc(s.project) + '</div>'
        + '<div class="sess-dur">' + fmt(s.dur) + '</div>'
        + '<div class="sess-int-wrap"><div class="sess-int-bar"><div class="sess-int-fill" style="width:' + ipct + '%"></div></div></div>'
        + '</div>';
  });
  $('sessions').innerHTML = ss || '<div style="color:var(--fg2)">—</div>';

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

async function refresh() {
  try {
    const r = await fetch('/api/live');
    if (r.ok) update(await r.json());
  } catch (e) {
    console.error('Refresh error:', e);
  }
}

refresh();
setInterval(refresh, REFRESH);
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=7880, debug=False)
