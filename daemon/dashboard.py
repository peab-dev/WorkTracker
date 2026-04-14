#!/usr/bin/env python3
"""WorkTracker Live Dashboard — Terminal UI"""

import curses
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import yaml

BASE = Path.home() / "WorkTracker"
DATA_SNAP = BASE / "data" / "snapshots"
DATA_SESS = BASE / "data" / "sessions"
SUMMARIES = BASE / "summaries"
LOGS = BASE / "logs"
CONFIG_PATH = BASE / "daemon" / "config.yaml"
CONFIG_DEFAULT_PATH = BASE / "daemon" / "config.default.yaml"


def _ensure_user_config() -> None:
    """Bootstrap the gitignored user config from the committed default."""
    if CONFIG_PATH.exists() or not CONFIG_DEFAULT_PATH.exists():
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(CONFIG_DEFAULT_PATH.read_text())


def load_config():
    """Load config.yaml and return relevant values."""
    try:
        _ensure_user_config()
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        col = cfg.get("collector", {})
        agg = cfg.get("aggregator", {})
        return {
            "interval": col.get("interval_seconds", 10),
            "min_snapshots": agg.get("min_session_snapshots", 2),
            "idle_threshold": agg.get("idle_threshold_seconds", 120),
        }
    except Exception:
        return {"interval": 10, "min_snapshots": 2, "idle_threshold": 120}

REFRESH_MS = 2000

# ── Load data ────────────────────────────────────────────────


def tail_jsonl(path, n=6):
    """Liest die letzten n Zeilen einer JSONL-Datei (effizient von hinten)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            if fsize == 0:
                return []
            read_size = min(fsize, n * 8192)
            f.seek(max(0, fsize - read_size))
            lines = f.read().decode("utf-8", errors="replace").strip().split("\n")
            lines = [l for l in lines if l.strip()]
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
    """Load session data for a date."""
    path = DATA_SESS / f"{date_str}.json"
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def snapshot_count_today(date_str):
    """Count snapshots for today."""
    path = DATA_SNAP / f"{date_str}.jsonl"
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def get_launchd_info(label):
    """Check launchd job status."""
    try:
        r = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return None
        info = {"pid": None, "exit": None}
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
        return None


def latest_report(report_type):
    """Findet den neuesten Report eines Typs."""
    d = SUMMARIES / report_type
    try:
        files = sorted(d.glob("*.md"))
        if files:
            f = files[-1]
            st = f.stat()
            return f.name, st.st_size, st.st_mtime
    except Exception:
        pass
    return None, 0, 0


def fmt_age(mtime):
    """Format file age as human-readable string."""
    if not mtime:
        return ""
    sec = int(time.time() - mtime)
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


def log_tail(path, n=1):
    """Liest die letzte Zeile einer Log-Datei."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            if fsize == 0:
                return ""
            f.seek(max(0, fsize - 2048))
            lines = f.read().decode("utf-8", errors="replace").strip().split("\n")
            return lines[-1] if lines else ""
    except FileNotFoundError:
        return ""


# ── Hilfsfunktionen ─────────────────────────────────────────


def fmt_dur(seconds):
    """Formatiert Sekunden als 'Xh XXm' oder 'Xm'."""
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}h {m:02d}m" if h > 0 else f"{m}m"


def bar(value, max_val, width=10):
    """Erzeugt eine Unicode-Balkenanzeige."""
    if max_val <= 0:
        return "░" * width
    filled = min(int(value / max_val * width), width)
    return "█" * filled + "░" * (width - filled)


def put(win, y, x, text, attr=0):
    """Sicheres Schreiben in ein curses-Fenster."""
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    text = str(text)[: w - x - 1]
    if not text:
        return
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def hline(win, y, w, attr=0):
    """Zeichnet eine horizontale Linie."""
    put(win, y, 0, "─" * w, attr)


# ── Dashboard ────────────────────────────────────────────────


def draw(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(REFRESH_MS)

    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    curses.init_pair(6, curses.COLOR_WHITE, -1)
    curses.init_pair(7, curses.COLOR_BLUE, -1)

    GRN = curses.color_pair(1)
    RED = curses.color_pair(2)
    YEL = curses.color_pair(3)
    CYN = curses.color_pair(4)
    MAG = curses.color_pair(5)
    WHT = curses.color_pair(6)
    BLU = curses.color_pair(7)
    B = curses.A_BOLD
    D = curses.A_DIM

    while True:
        stdscr.erase()
        H, W = stdscr.getmaxyx()
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if H < 20 or W < 60:
            put(stdscr, 0, 0, "Terminal zu klein — mind. 60×20", RED | B)
            stdscr.refresh()
            if stdscr.getch() in (ord("q"), 27):
                break
            continue

        # ── Daten sammeln ──
        cfg = load_config()
        interval = cfg["interval"]
        snaps = tail_jsonl(DATA_SNAP / f"{today}.jsonl", 6)
        latest = snaps[-1] if snaps else None
        sessions = load_sessions(today)
        snap_total = snapshot_count_today(today)

        col_info = get_launchd_info("com.peab.worktracker.collector")
        agg_d = get_launchd_info("com.peab.worktracker.aggregator.daily")
        agg_w = get_launchd_info("com.peab.worktracker.aggregator.weekly")
        agg_m = get_launchd_info("com.peab.worktracker.aggregator.monthly")

        r = 0

        # ── Header ──
        hline(stdscr, r, W, D)
        r += 1
        put(stdscr, r, 2, " WORKTRACKER LIVE DASHBOARD ", CYN | B)
        ts_str = now.strftime("%d.%m.%Y  %H:%M:%S")
        put(stdscr, r, W - len(ts_str) - 2, ts_str, D)
        r += 1
        hline(stdscr, r, W, D)
        r += 1

        # ── Daemon Status ──
        put(stdscr, r, 2, "SERVICES", CYN | B)
        snap_info = f"  {snap_total} Snapshots heute" if snap_total else ""
        put(stdscr, r, 14, snap_info, D)
        r += 1

        # Collector
        if col_info:
            pid = col_info.get("pid")
            if pid:
                put(stdscr, r, 2, "●", GRN | B)
                put(stdscr, r, 4, "Collector", WHT | B)
                put(stdscr, r, 22, f"{interval}s interval  PID {pid}", GRN)
            else:
                put(stdscr, r, 2, "●", GRN)
                put(stdscr, r, 4, "Collector", WHT | B)
                put(stdscr, r, 22, f"{interval}s interval  (idle)", YEL)
        else:
            put(stdscr, r, 2, "○", RED)
            put(stdscr, r, 4, "Collector", WHT)
            put(stdscr, r, 22, "not loaded", RED)
        r += 1

        for info, name, sched in [
            (agg_d, "Agg Daily  ", "daily 22:00"),
            (agg_w, "Agg Weekly ", "Sun 22:30"),
            (agg_m, "Agg Monthly", "1st of month 00:30"),
        ]:
            if info:
                ex = info.get("exit", "—")
                put(stdscr, r, 2, "◆", BLU)
                put(stdscr, r, 4, name, WHT)
                put(stdscr, r, 22, f"ready  {sched}", D)
                ec_col = GRN if ex == 0 else YEL
                put(stdscr, r, 46, f"exit={ex}", ec_col)
            else:
                put(stdscr, r, 2, "○", RED)
                put(stdscr, r, 4, name, WHT)
                put(stdscr, r, 22, "not loaded", RED)
            r += 1

        r += 1
        hline(stdscr, r, W, D)
        r += 1

        # ── Current Activity ──
        put(stdscr, r, 2, "LIVE", CYN | B)
        r += 1

        if latest:
            app = latest.get("active_app", {})
            app_name = app.get("name", "—")
            win_title = app.get("window_title", "—")

            put(stdscr, r, 2, "App:", D)
            put(stdscr, r, 10, app_name, WHT | B)
            r += 1
            put(stdscr, r, 2, "Fenster:", D)
            put(stdscr, r, 10, (win_title or "—")[: W - 12], WHT)
            r += 1

            # Media
            media = latest.get("media")
            if media and isinstance(media, dict) and media.get("title"):
                svc = media.get("service") or media.get("app") or ""
                mt = media.get("title", "")
                if media.get("artist"):
                    mt += f" — {media['artist']}"
                put(stdscr, r, 2, f"♫ {svc}:", MAG)
                put(stdscr, r, 4 + len(svc) + 2, mt[: W - 20], MAG | D)
                r += 1

            r += 1

            # Input-Raten
            inp = latest.get("input", {})
            if len(snaps) >= 2:
                span = len(snaps) * interval
                tot_keys = sum(s.get("input", {}).get("keystrokes", 0) for s in snaps)
                tot_clicks = sum(
                    s.get("input", {}).get("mouse_clicks_left", 0)
                    + s.get("input", {}).get("mouse_clicks_right", 0)
                    for s in snaps
                )
                tot_scroll = sum(s.get("input", {}).get("scroll_events", 0) for s in snaps)
                kpm = int(tot_keys * 60 / span)
                cpm = int(tot_clicks * 60 / span)
                spm = int(tot_scroll * 60 / span)

                put(stdscr, r, 2, "Keys:", D)
                put(stdscr, r, 10, bar(kpm, 200, 10), GRN if kpm > 0 else D)
                put(stdscr, r, 21, f"{kpm}/min", WHT)

                put(stdscr, r, 32, "Clicks:", D)
                put(stdscr, r, 40, bar(cpm, 80, 10), GRN if cpm > 0 else D)
                put(stdscr, r, 51, f"{cpm}/min", WHT)

                if W > 72:
                    put(stdscr, r, 62, "Scroll:", D)
                    put(stdscr, r, 70, f"{spm}/min", WHT)
                r += 1

            # Idle
            idle_kb = inp.get("idle_seconds_keyboard", 0)
            idle_ms = inp.get("idle_seconds_mouse", 0)
            if idle_kb > 60 or idle_ms > 60:
                idle_max = max(idle_kb, idle_ms)
                put(stdscr, r, 2, f"⏸  Idle for {int(idle_max)}s", YEL)
                r += 1

            # System
            sys_info = latest.get("system", {})
            parts = []
            bp = sys_info.get("battery_pct")
            if bp is not None:
                batt = f"Akku: {bp}%"
                if sys_info.get("battery_charging"):
                    batt += " ⚡"
                parts.append(batt)
            br = sys_info.get("brightness")
            if br is not None:
                parts.append(f"Helligkeit: {int(br * 100)}%")
            sp = sys_info.get("active_space")
            if sp is not None:
                parts.append(f"Space: {sp}")
            if parts:
                put(stdscr, r, 2, "  ".join(parts), D)
                r += 1

            # Git
            git = latest.get("git")
            if git and isinstance(git, dict) and git.get("repo"):
                put(stdscr, r, 2, f"Git: {git['repo']}/{git.get('branch', '—')}", BLU | D)
                r += 1

            # Config values
            r += 1
            min_ses = cfg["min_snapshots"]
            min_dur = min_ses * interval
            put(stdscr, r, 2, f"Interval: {interval}s", D)
            put(stdscr, r, 18, f"Min Session: {min_ses}×{interval}s = {min_dur}s", D)
            put(stdscr, r, 46, f"Idle: {cfg['idle_threshold']}s", D)
            r += 1
        else:
            put(stdscr, r, 2, "No snapshot data", YEL)
            r += 2

        r += 1
        hline(stdscr, r, W, D)
        r += 1

        # ── Daily Overview ──
        put(stdscr, r, 2, f"TODAY  {now.strftime('%Y-%m-%d')}", CYN | B)
        r += 1

        if sessions:
            total_sec = sum(s.get("duration_seconds", 0) for s in sessions)
            focus = [s for s in sessions if s.get("duration_seconds", 0) >= 1500]
            focus_sec = sum(s.get("duration_seconds", 0) for s in focus)

            apps = [s.get("app_name", "") for s in sessions]
            switches = sum(1 for i in range(1, len(apps)) if apps[i] != apps[i - 1])
            hrs = total_sec / 3600 if total_sec > 0 else 1
            sph = int(switches / hrs)

            clip_total = sum(len(s.get("clipboard_events", [])) for s in sessions)
            total_keys = sum(s.get("keystrokes_total", 0) for s in sessions)
            total_clicks = sum(s.get("mouse_clicks_total", 0) for s in sessions)

            put(stdscr, r, 2, "Active:", D)
            put(stdscr, r, 10, fmt_dur(total_sec), WHT | B)
            put(stdscr, r, 20, "Sessions:", D)
            put(stdscr, r, 30, str(len(sessions)), WHT | B)
            put(stdscr, r, 36, "Focus:", D)
            foc_txt = f"{len(focus)} ({fmt_dur(focus_sec)})"
            put(stdscr, r, 43, foc_txt, GRN | B if focus else D)
            r += 1

            put(stdscr, r, 2, "Switches:", D)
            put(stdscr, r, 12, f"{switches} ({sph}/h)", WHT)
            put(stdscr, r, 30, "Keys:", D)
            put(stdscr, r, 39, f"{total_keys:,}", WHT)
            put(stdscr, r, 50, "Clipboard:", D)
            put(stdscr, r, 61, f"{clip_total}x", WHT)
            r += 2

            # Projects
            projects = {}
            for s in sessions:
                p = s.get("project", "Other")
                if p not in projects:
                    projects[p] = {"sec": 0, "n": 0, "inten": []}
                projects[p]["sec"] += s.get("duration_seconds", 0)
                projects[p]["n"] += 1
                isc = s.get("intensity_score")
                if isc is not None:
                    projects[p]["inten"].append(isc)

            sorted_proj = sorted(projects.items(), key=lambda x: x[1]["sec"], reverse=True)

            put(stdscr, r, 2, "Project", D | B)
            put(stdscr, r, 22, "Time", D | B)
            put(stdscr, r, 31, "%", D | B)
            put(stdscr, r, 37, "Sess", D | B)
            put(stdscr, r, 44, "Intensity", D | B)
            r += 1

            for pname, pd in sorted_proj[:7]:
                if r >= H - 12:
                    break
                pct = pd["sec"] / total_sec * 100 if total_sec > 0 else 0
                avg_i = sum(pd["inten"]) / len(pd["inten"]) if pd["inten"] else 0
                put(stdscr, r, 2, pname[:18], WHT)
                put(stdscr, r, 22, fmt_dur(pd["sec"]), WHT)
                put(stdscr, r, 31, f"{pct:4.0f}%", D)
                put(stdscr, r, 37, f"{pd['n']:4d}", D)
                put(stdscr, r, 44, bar(avg_i, 10, 8), GRN)
                put(stdscr, r, 53, f"{avg_i:.1f}", D)
                r += 1

            r += 1
            hline(stdscr, r, W, D)
            r += 1

            # Recent Sessions
            put(stdscr, r, 2, "LETZTE SESSIONS", CYN | B)
            r += 1

            recent = list(reversed(sessions[-8:]))
            for s in recent:
                if r >= H - 5:
                    break
                try:
                    t = datetime.fromisoformat(s["start"]).strftime("%H:%M")
                except Exception:
                    t = "—:—"
                sapp = s.get("app_name", "—")[:14]
                stitle = s.get("window_title", "—") or "—"
                sdur = fmt_dur(s.get("duration_seconds", 0))
                si = s.get("intensity_score", 0)
                sproj = s.get("project", "")[:10]

                put(stdscr, r, 2, t, D)
                put(stdscr, r, 8, sapp, WHT | B)
                title_max = W - 56
                if title_max > 5:
                    put(stdscr, r, 24, stitle[:title_max], WHT)
                put(stdscr, r, max(W - 28, 50), sproj, BLU | D)
                put(stdscr, r, max(W - 16, 60), sdur, YEL)
                put(stdscr, r, max(W - 10, 66), bar(si, 10, 5), GRN)
                put(stdscr, r, max(W - 4, 72), f"{si:.0f}", D)
                r += 1
        else:
            put(stdscr, r, 2, "No sessions for today yet", YEL)
            r += 2

        r += 1
        hline(stdscr, r, W, D)
        r += 1

        # ── Reports ──
        put(stdscr, r, 2, "REPORTS", CYN | B)
        r += 1
        for rtype, label in [("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")]:
            fname, fsize, fmtime = latest_report(rtype)
            put(stdscr, r, 2, f"  {label}:", D)
            if fname:
                put(stdscr, r, 14, fname, WHT)
                put(stdscr, r, 32, f"({fsize / 1024:.1f} KB)", D)
                age = fmt_age(fmtime)
                if age:
                    put(stdscr, r, 46, age, D)
            else:
                put(stdscr, r, 14, "—", D)
            r += 1

        # ── Log-Zeile ──
        r += 1
        last_log = log_tail(LOGS / "collector.log")
        if last_log:
            put(stdscr, r, 2, "Log:", D)
            put(stdscr, r, 7, last_log[: W - 9], D)
            r += 1

        # ── Footer ──
        hline(stdscr, H - 2, W, D)
        put(stdscr, H - 1, 2, "q", WHT | B)
        put(stdscr, H - 1, 3, "=Beenden  ", D)
        put(stdscr, H - 1, 13, "r", WHT | B)
        put(stdscr, H - 1, 14, "=Refresh  ", D)
        put(stdscr, H - 1, 24, f"↻ {REFRESH_MS / 1000:.0f}s", D)

        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            break


def main():
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(draw)


if __name__ == "__main__":
    main()
