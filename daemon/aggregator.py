#!/usr/bin/env python3
"""WorkTracker Aggregator — processes Collector snapshots into sessions and Markdown reports."""

import argparse
import fnmatch
import json
import logging
import logging.handlers
import math
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import pandas as pd
import yaml
from rapidfuzz.fuzz import ratio as levenshtein_ratio

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path.home() / "WorkTracker"
CONFIG_PATH = BASE_DIR / "daemon" / "config.yaml"
PATTERNS_PATH = BASE_DIR / "daemon" / "project_patterns.yaml"
SNAPSHOTS_DIR = BASE_DIR / "data" / "snapshots"
SESSIONS_DIR = BASE_DIR / "data" / "sessions"
DAILY_DIR = BASE_DIR / "summaries" / "daily"
WEEKLY_DIR = BASE_DIR / "summaries" / "weekly"
MONTHLY_DIR = BASE_DIR / "summaries" / "monthly"
LOG_DIR = BASE_DIR / "logs"
TZ = "Europe/Vienna"

def _safe_int(val, default: int = 0) -> int:
    """Convert value to int, handling NaN, None, and empty strings."""
    if val is None:
        return default
    try:
        if isinstance(val, float) and math.isnan(val):
            return default
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_str(val, default: str = "") -> str:
    """Convert value to str, handling NaN, None, and non-string types."""
    if val is None:
        return default
    if isinstance(val, float):
        if math.isnan(val):
            return default
        return str(val)
    if isinstance(val, str):
        return val
    return str(val)


SYSTEM_APPS = {
    "WindowManager", "Finder", "Dock", "SystemUIServer",
    "Control Center", "Notification Center", "loginwindow",
    "Spotlight", "Passwords",
}

# Apps that represent inactive/lock-screen state — excluded from active time stats
INACTIVE_APPS = {"loginwindow"}

WEEKDAYS = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
    4: "Friday", 5: "Saturday", 6: "Sunday",
}
MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aggregator")
    logger.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "aggregator.log", maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(sh)
    return logger


log = setup_logging()

# ---------------------------------------------------------------------------
# Config & Patterns
# ---------------------------------------------------------------------------


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def load_patterns() -> tuple[dict[str, dict], str]:
    """Return (projects_dict, default_project).

    Loads static patterns from *project_patterns.yaml* and, if it exists,
    merges learned patterns from *learned_patterns.yaml* (static wins on
    conflict).
    """
    with open(PATTERNS_PATH) as f:
        data = yaml.safe_load(f) or {}

    # Merge learned patterns (auto-generated suggestions)
    learned_path = PATTERNS_PATH.parent / "learned_patterns.yaml"
    if learned_path.exists():
        try:
            with open(learned_path) as f:
                learned = yaml.safe_load(f) or {}
            for proj_name, proj_info in learned.get("projects", {}).items():
                if proj_name not in data.get("projects", {}):
                    data.setdefault("projects", {})[proj_name] = proj_info
        except Exception:
            log.warning("Failed to load learned_patterns.yaml", exc_info=True)

    return data.get("projects", {}), data.get("default_project", "Other")


def match_project(
    title: str,
    projects: dict[str, dict],
    default: str,
    url: str = "",
    app_name: str = "",
) -> tuple[str, str]:
    """Match window title, URL, and app name against project patterns.

    Returns *(project_name, category)*.  Priority: title → url → app_name.
    """
    title = _safe_str(title)
    url = _safe_str(url)
    app_name = _safe_str(app_name)
    if not title and not url and not app_name:
        return default, ""
    title_lower = title.lower()
    url_lower = url.lower()
    app_lower = app_name.lower()
    for proj_name, proj_info in projects.items():
        # Title patterns
        for pattern in proj_info.get("patterns", []):
            if fnmatch.fnmatch(title_lower, pattern.lower()):
                return proj_name, proj_info.get("category", "")
        # URL patterns
        if url_lower:
            for pattern in proj_info.get("url_patterns", []):
                if fnmatch.fnmatch(url_lower, pattern.lower()):
                    return proj_name, proj_info.get("category", "")
        # App name patterns
        if app_lower:
            for pattern in proj_info.get("app_patterns", []):
                if fnmatch.fnmatch(app_lower, pattern.lower()):
                    return proj_name, proj_info.get("category", "")
    if app_name:
        return f"{default} ({app_name})", ""
    return default, ""


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------


def load_snapshots(date: datetime) -> pd.DataFrame:
    """Load a single day's JSONL into a DataFrame."""
    path = SNAPSHOTS_DIR / f"{date.strftime('%Y-%m-%d')}.jsonl"
    if not path.exists():
        log.warning("No data for %s", date.strftime("%Y-%m-%d"))
        return pd.DataFrame()
    df = pd.read_json(path, lines=True)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(TZ)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def load_date_range(start: datetime, end: datetime) -> pd.DataFrame:
    """Load all JSONL files in a date range (inclusive)."""
    frames = []
    d = start
    while d <= end:
        day_df = load_snapshots(d)
        if not day_df.empty:
            frames.append(day_df)
        d += timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values("ts").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Flatten nested snapshot fields
# ---------------------------------------------------------------------------


def flatten_df(df: pd.DataFrame) -> pd.DataFrame:
    """Extract nested fields into flat columns."""
    if df.empty:
        return df

    # active_app
    aa = pd.json_normalize(df["active_app"].tolist()).add_prefix("app_")
    aa.index = df.index
    df = pd.concat([df, aa], axis=1)

    # input
    inp = pd.json_normalize(df["input"].tolist()).add_prefix("input_")
    inp.index = df.index
    df = pd.concat([df, inp], axis=1)

    # clipboard
    cb = pd.json_normalize(df["clipboard"].tolist()).add_prefix("clip_")
    cb.index = df.index
    df = pd.concat([df, cb], axis=1)

    # system
    sys_norm = pd.json_normalize(df["system"].tolist()).add_prefix("sys_")
    sys_norm.index = df.index
    df = pd.concat([df, sys_norm], axis=1)

    # media (can be None)
    media_records = []
    for m in df["media"]:
        if m is None:
            media_records.append({})
        else:
            media_records.append(m)
    med = pd.json_normalize(media_records).add_prefix("media_")
    med.index = df.index
    df = pd.concat([df, med], axis=1)

    # git (can be None)
    if "git" in df.columns:
        git_records = [g if g else {} for g in df["git"]]
        git_norm = pd.json_normalize(git_records).add_prefix("git_")
        git_norm.index = df.index
        df = pd.concat([df, git_norm], axis=1)

    # calendar (can be None)
    if "calendar" in df.columns:
        cal_records = [c if c else {} for c in df["calendar"]]
        cal_norm = pd.json_normalize(cal_records).add_prefix("cal_")
        cal_norm.index = df.index
        df = pd.concat([df, cal_norm], axis=1)

    # sleep_wake — keep as-is (list or None), no flattening needed
    # The column is used directly by _row_has_sleep_wake()

    return df


# ---------------------------------------------------------------------------
# Session Detection
# ---------------------------------------------------------------------------


def detect_sessions(df: pd.DataFrame, cfg: dict) -> list[dict]:
    """Group consecutive snapshots into sessions based on app, title similarity, and idle gap.

    Uses a two-tier merging strategy to reduce fragmentation:
    - Tier 1: Same app + small gap → always merge (covers rapid tab switching)
    - Tier 2: Same app + larger gap → require title similarity
    Sleep/wake events always force a session split.
    """
    if df.empty:
        return []

    agg_cfg = cfg.get("aggregator", {})
    idle_threshold = agg_cfg.get("idle_threshold_seconds", 120)
    fuzzy_threshold = agg_cfg.get("fuzzy_match_threshold", 0.7) * 100  # rapidfuzz uses 0-100
    same_app_grace = agg_cfg.get("same_app_grace_period_seconds", 30)
    min_snapshots = agg_cfg.get("min_session_snapshots", 2)

    projects, default_project = load_patterns()
    interval = cfg.get("collector", {}).get("interval_seconds", 10)

    sessions: list[dict] = []
    current: dict[str, Any] | None = None

    for _, row in df.iterrows():
        app_name = _safe_str(row.get("app_name"))
        title = _safe_str(row.get("app_window_title"))
        ts = row["ts"]

        if current is None:
            current = _new_session(row, projects, default_project)
            continue

        # Sleep/wake event → always split
        has_gap = _row_has_sleep_wake(row)

        time_gap = (ts - current["_last_ts"]).total_seconds()
        same_app = app_name == current["app_name"]
        title_similar = levenshtein_ratio(title, current["_last_title"]) >= fuzzy_threshold
        idle_kb = row.get("input_idle_seconds_keyboard", 0) or 0
        idle_ms = row.get("input_idle_seconds_mouse", 0) or 0
        is_idle = min(idle_kb, idle_ms) > idle_threshold

        if has_gap:
            # Force split on sleep/wake gap
            sessions.append(_finalize_session(current))
            current = _new_session(row, projects, default_project)
        elif same_app and time_gap < same_app_grace and not is_idle:
            # Tier 1: same app + small gap → merge (tab switching)
            current["end"] = ts
            current["_titles"].append(title)
            _accumulate_session(current, row, interval)
        elif same_app and title_similar and time_gap < idle_threshold and not is_idle:
            # Tier 2: same app + larger gap → require title similarity
            current["end"] = ts
            current["_titles"].append(title)
            _accumulate_session(current, row, interval)
        else:
            # Different app, long gap, or idle → split
            sessions.append(_finalize_session(current))
            current = _new_session(row, projects, default_project)

    if current is not None:
        sessions.append(_finalize_session(current))

    # Post-process: absorb micro-sessions into neighbours
    if min_snapshots > 1 and len(sessions) > 1:
        sessions = _merge_micro_sessions(sessions, min_snapshots)

    return sessions


def _row_has_sleep_wake(row: pd.Series) -> bool:
    """Check if a snapshot row contains sleep/wake events."""
    val = row.get("sleep_wake")
    if val is None:
        return False
    if isinstance(val, float) and math.isnan(val):
        return False
    if isinstance(val, list) and len(val) > 0:
        return True
    return False


def _merge_micro_sessions(sessions: list[dict], min_snapshots: int) -> list[dict]:
    """Absorb sessions with fewer than *min_snapshots* into adjacent same-app sessions."""
    merged: list[dict] = []
    for s in sessions:
        if (
            s["snapshot_count"] < min_snapshots
            and merged
            and merged[-1]["app_name"] == s["app_name"]
        ):
            prev = merged[-1]
            prev["end"] = s["end"]
            prev["duration_seconds"] = (
                pd.Timestamp(prev["end"]) - pd.Timestamp(prev["start"])
            ).total_seconds()
            prev["snapshot_count"] += s["snapshot_count"]
            prev["keystrokes_total"] += s["keystrokes_total"]
            prev["mouse_clicks_total"] += s["mouse_clicks_total"]
            prev["scroll_events_total"] += s["scroll_events_total"]
            # Recalculate intensity
            dur = max(prev["duration_seconds"], 10)
            raw = (prev["keystrokes_total"] + prev["mouse_clicks_total"]) / dur
            prev["intensity_score"] = round(min(10.0, raw * 10), 1)
        else:
            merged.append(s)
    return merged


def _new_session(row: pd.Series, projects: dict, default_project: str) -> dict:
    title = _safe_str(row.get("app_window_title"))
    url = _safe_str(row.get("app_url"))
    app_name = _safe_str(row.get("app_name"))
    proj, cat = match_project(title, projects, default_project, url=url, app_name=app_name)
    interval = 10
    return {
        "start": row["ts"],
        "end": row["ts"],
        "app_name": row.get("app_name") or "",
        "app_bundle_id": row.get("app_bundle_id") or "",
        "_titles": [title],
        "_last_title": title,
        "_last_ts": row["ts"],
        "project": proj,
        "category": cat,
        "keystrokes_total": _safe_int(row.get("input_keystrokes")),
        "mouse_clicks_total": _safe_int(row.get("input_mouse_clicks_left"))
                              + _safe_int(row.get("input_mouse_clicks_right")),
        "scroll_events_total": _safe_int(row.get("input_scroll_events")),
        "_clipboard_events": [],
        "_media_snapshots": [],
        "_snapshot_count": 1,
        "_urls": [url] if url and pd.notna(url) else [],
        "_git_repo": row.get("git_repo", ""),
        "_git_branch": row.get("git_branch", ""),
    }


def _accumulate_session(session: dict, row: pd.Series, interval: int) -> None:
    session["_last_title"] = _safe_str(row.get("app_window_title"))
    session["_last_ts"] = row["ts"]
    session["keystrokes_total"] += _safe_int(row.get("input_keystrokes"))
    session["mouse_clicks_total"] += _safe_int(row.get("input_mouse_clicks_left")) \
                                     + _safe_int(row.get("input_mouse_clicks_right"))
    session["scroll_events_total"] += _safe_int(row.get("input_scroll_events"))
    session["_snapshot_count"] += 1

    # URLs
    url = row.get("app_url", "")
    if url and pd.notna(url):
        session["_urls"].append(url)

    # Git
    git_repo = row.get("git_repo", "")
    if git_repo and pd.notna(git_repo):
        session["_git_repo"] = git_repo
        session["_git_branch"] = row.get("git_branch", "")

    # Clipboard events
    if row.get("clip_changed"):
        session["_clipboard_events"].append({
            "source_app": row.get("clip_source_app", ""),
            "type": row.get("clip_type", ""),
            "length": _safe_int(row.get("clip_length")),
        })

    # Media
    media_title = row.get("media_title")
    if media_title and pd.notna(media_title):
        session["_media_snapshots"].append({
            "title": media_title,
            "artist": row.get("media_artist", ""),
            "app": row.get("media_app", ""),
            "service": row.get("media_service", ""),
        })


def _finalize_session(session: dict) -> dict:
    duration = (session["end"] - session["start"]).total_seconds()
    # Minimum duration = 1 snapshot interval
    if duration < 10:
        duration = 10.0

    # Most common title
    title_counts = Counter(session["_titles"])
    window_title = title_counts.most_common(1)[0][0] if title_counts else ""

    # Intensity score: (keystrokes + clicks) / duration, normalized to 0-10
    raw_intensity = (session["keystrokes_total"] + session["mouse_clicks_total"]) / duration
    intensity_score = min(10.0, raw_intensity * 10)

    # Parallel media: most common
    parallel_media = None
    if session["_media_snapshots"]:
        media_titles = [m["title"] for m in session["_media_snapshots"]]
        most_common_media = Counter(media_titles).most_common(1)[0][0]
        for m in session["_media_snapshots"]:
            if m["title"] == most_common_media:
                parallel_media = m
                break

    # Most common URL
    urls = [u for u in session.get("_urls", []) if u]
    url = Counter(urls).most_common(1)[0][0] if urls else None

    # Git info
    git_repo = session.get("_git_repo", "")
    git_branch = session.get("_git_branch", "")

    result = {
        "start": session["start"].isoformat(),
        "end": session["end"].isoformat(),
        "duration_seconds": round(duration),
        "app_name": session["app_name"],
        "app_bundle_id": session["app_bundle_id"],
        "window_title": window_title,
        "project": session["project"],
        "category": session["category"],
        "keystrokes_total": session["keystrokes_total"],
        "mouse_clicks_total": session["mouse_clicks_total"],
        "scroll_events_total": session["scroll_events_total"],
        "intensity_score": round(intensity_score, 1),
        "clipboard_events": session["_clipboard_events"],
        "parallel_media": parallel_media,
        "snapshot_count": session["_snapshot_count"],
    }
    if url:
        result["url"] = url
    if git_repo:
        result["git_repo"] = git_repo
        result["git_branch"] = git_branch
    return result


# ---------------------------------------------------------------------------
# Pandas Calculations
# ---------------------------------------------------------------------------


def calc_daily_stats(df: pd.DataFrame, sessions: list[dict], cfg: Optional[dict] = None) -> dict:
    """Compute all daily statistics from DataFrame and sessions."""
    stats: dict[str, Any] = {}
    if cfg is None:
        cfg = {}
    if df.empty or not sessions:
        return stats

    sdf = pd.DataFrame(sessions)
    sdf["start_dt"] = pd.to_datetime(sdf["start"], format="ISO8601")
    sdf["end_dt"] = pd.to_datetime(sdf["end"], format="ISO8601")
    sdf["start_hour"] = sdf["start_dt"].dt.hour

    # Filter out lock-screen / inactive apps from active time stats
    active_sdf = sdf[~sdf["app_name"].isin(INACTIVE_APPS)]

    # --- Overview ---
    total_active_sec = active_sdf["duration_seconds"].sum()
    first_ts = active_sdf["start_dt"].min() if not active_sdf.empty else sdf["start_dt"].min()
    last_ts = active_sdf["end_dt"].max() if not active_sdf.empty else sdf["end_dt"].max()
    focus_sessions = active_sdf[active_sdf["duration_seconds"] > 1500]
    focus_count = len(focus_sessions)
    focus_total_sec = focus_sessions["duration_seconds"].sum() if focus_count > 0 else 0

    # App switches in raw data (exclude loginwindow from switch counting)
    active_df = df[~df["app_name"].isin(INACTIVE_APPS)]
    app_switches = (active_df["app_name"] != active_df["app_name"].shift(1)).sum() - 1
    app_switches = max(0, app_switches)
    hours_span = max(1, (last_ts - first_ts).total_seconds() / 3600)
    switches_per_hour = round(app_switches / hours_span)

    # Idle ratio
    idle_threshold = 30
    idle_kb = df.get("input_idle_seconds_keyboard", pd.Series(dtype=float))
    idle_ms = df.get("input_idle_seconds_mouse", pd.Series(dtype=float))
    if not idle_kb.empty and not idle_ms.empty:
        idle_snapshots = ((idle_kb > idle_threshold) & (idle_ms > idle_threshold)).sum()
        idle_ratio = idle_snapshots / len(df) if len(df) > 0 else 0
    else:
        idle_ratio = 0

    # Parallel media ratio
    media_while_working = 0
    for _, row in df.iterrows():
        mt = row.get("media_title")
        if mt and pd.notna(mt):
            media_app = row.get("media_app", "")
            active_app = row.get("app_name", "")
            if media_app != active_app:
                media_while_working += 1
    media_ratio = media_while_working / len(df) if len(df) > 0 else 0

    stats["overview"] = {
        "total_active_seconds": int(total_active_sec),
        "time_range_start": first_ts.strftime("%H:%M"),
        "time_range_end": last_ts.strftime("%H:%M"),
        "focus_session_count": focus_count,
        "focus_total_seconds": int(focus_total_sec),
        "app_switches": int(app_switches),
        "switches_per_hour": int(switches_per_hour),
        "idle_ratio": round(idle_ratio, 2),
        "media_ratio": round(media_ratio, 2),
    }

    # --- Project distribution (excluding inactive apps) ---
    proj_group = active_sdf.groupby("project").agg(
        total_seconds=("duration_seconds", "sum"),
        session_count=("duration_seconds", "count"),
        avg_duration=("duration_seconds", "mean"),
        avg_intensity=("intensity_score", "mean"),
    ).sort_values("total_seconds", ascending=False)
    proj_group["pct"] = proj_group["total_seconds"] / max(total_active_sec, 1)
    stats["projects"] = proj_group.reset_index().to_dict("records")

    # --- App usage (excluding inactive apps) ---
    app_group = active_sdf.groupby("app_name").agg(
        total_seconds=("duration_seconds", "sum"),
        top_project=("project", lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else ""),
    ).sort_values("total_seconds", ascending=False)
    app_group["pct"] = app_group["total_seconds"] / max(total_active_sec, 1)
    stats["apps"] = app_group.reset_index().to_dict("records")

    # --- Timeline (excluding inactive apps) ---
    stats["timeline"] = [s for s in sessions if s.get("app_name") not in INACTIVE_APPS]

    # --- Parallel media ---
    media_entries = []
    for s in sessions:
        if s.get("parallel_media"):
            media_entries.append({
                "start": s["start"],
                "end": s["end"],
                "media": s["parallel_media"],
                "app": s["app_name"],
            })
    stats["parallel_media"] = media_entries

    # --- Input analysis ---
    if "input_keystrokes" in df.columns:
        hourly_keys = df.groupby(df["ts"].dt.hour)["input_keystrokes"].sum()
        stats["hourly_keystrokes"] = {int(h): int(v) for h, v in hourly_keys.items()}
    else:
        stats["hourly_keystrokes"] = {}

    total_keystrokes = int(df.get("input_keystrokes", pd.Series([0])).sum())
    total_clicks = int(df.get("input_mouse_clicks_left", pd.Series([0])).sum()) + \
                   int(df.get("input_mouse_clicks_right", pd.Series([0])).sum())
    stats["input"] = {
        "total_keystrokes": total_keystrokes,
        "total_clicks": total_clicks,
        "peak_hour": int(max(stats["hourly_keystrokes"], key=stats["hourly_keystrokes"].get)) if stats["hourly_keystrokes"] else 0,
    }

    # --- Clipboard transfers ---
    clip_transfers: dict[str, int] = {}
    for s in sessions:
        for ce in s.get("clipboard_events", []):
            src = ce.get("source_app", "Unknown")
            dst = s["app_name"]
            key = f"{src} → {dst}"
            clip_transfers[key] = clip_transfers.get(key, 0) + 1
    stats["clipboard_transfers"] = [
        {"route": k, "count": v}
        for k, v in sorted(clip_transfers.items(), key=lambda x: -x[1])
    ]

    # --- Patterns ---
    patterns = []
    if focus_count > 0:
        longest_focus = focus_sessions.loc[focus_sessions["duration_seconds"].idxmax()]
        patterns.append(
            f"Longest focus session: {fmt_duration(longest_focus['duration_seconds'])} "
            f"({longest_focus['app_name']} — {longest_focus['project']})"
        )
    # Highest app switch rate per hour (excluding inactive apps)
    if "app_name" in active_df.columns:
        hourly_switches = active_df.groupby(active_df["ts"].dt.hour)["app_name"].apply(
            lambda x: (x != x.shift(1)).sum()
        )
        if not hourly_switches.empty:
            peak_switch_hour = int(hourly_switches.idxmax())
            patterns.append(
                f"Highest app switch rate: {peak_switch_hour}:00–{peak_switch_hour+1}:00 "
                f"({int(hourly_switches.max())} switches)"
            )
    stats["patterns"] = patterns

    # --- URL domain distribution ---
    url_domains: dict[str, int] = {}
    for s in sessions:
        url = s.get("url", "")
        if url:
            try:
                domain = urlparse(url).netloc
                if domain:
                    dur = s.get("duration_seconds", 0)
                    url_domains[domain] = url_domains.get(domain, 0) + dur
            except Exception:
                pass
    stats["url_domains"] = [
        {"domain": k, "total_seconds": v}
        for k, v in sorted(url_domains.items(), key=lambda x: -x[1])
    ]

    # --- Git activity ---
    git_commits = []
    git_repos_seen: set[str] = set()
    for s in sessions:
        repo = s.get("git_repo", "")
        if repo:
            git_repos_seen.add(repo)
    # Load git commits at aggregation time for more complete data
    cfg_collector = cfg.get("collector", {})
    if cfg_collector.get("track_git", False):
        for repo_path_str in cfg_collector.get("git_repos", []):
            repo_path = Path(os.path.expanduser(repo_path_str))
            if not (repo_path / ".git").exists():
                continue
            try:
                date_str = df["ts"].iloc[0].strftime("%Y-%m-%d")
                log_out = subprocess.run(
                    ["git", "log", f"--since={date_str} 00:00", f"--until={date_str} 23:59",
                     "--format=%H|%an|%s|%aI", "--no-merges"],
                    capture_output=True, text=True, timeout=10, cwd=repo_path,
                ).stdout.strip()
                for line in log_out.splitlines():
                    parts = line.split("|", 3)
                    if len(parts) == 4:
                        git_commits.append({
                            "repo": repo_path.name,
                            "hash": parts[0][:8],
                            "author": parts[1],
                            "message": parts[2],
                            "timestamp": parts[3],
                        })
            except Exception:
                pass
    stats["git_activity"] = {
        "commits": git_commits,
        "active_repos": sorted(git_repos_seen),
        "total_commits": len(git_commits),
    }

    # --- Calendar / time classification ---
    calendar_stats: dict[str, Any] = {
        "total_meeting_seconds": 0,
        "total_deep_work_seconds": 0,
        "total_shallow_work_seconds": 0,
        "meetings": [],
        "longest_meeting_free_block": 0,
    }
    if "cal_in_meeting" in df.columns:
        meeting_snapshots = df[df.get("cal_in_meeting", pd.Series(dtype=bool)) == True]
        meeting_seconds = len(meeting_snapshots) * cfg.get("collector", {}).get("interval_seconds", 10)
        calendar_stats["total_meeting_seconds"] = meeting_seconds

        # Extract unique meetings
        seen_meetings: set[str] = set()
        for _, row in meeting_snapshots.iterrows():
            title = row.get("cal_event_title", "")
            if title and title not in seen_meetings:
                seen_meetings.add(title)
                calendar_stats["meetings"].append({
                    "title": title,
                    "calendar": row.get("cal_event_calendar", ""),
                    "attendees": _safe_int(row.get("cal_attendee_count")),
                })

        non_meeting_sec = total_active_sec - meeting_seconds
        # Deep work: sessions > deep_work_min in productive categories during non-meeting time
        agg_cfg_cal = cfg.get("aggregator", {}).get("calendar_classification", {})
        deep_work_min = agg_cfg_cal.get("deep_work_min_minutes", 60) * 60
        productive_cats = {"Development", "AI/Research", "Business", "Creative", "Music"}
        deep_work_sec = 0
        for s in sessions:
            if s.get("category") in productive_cats and s["duration_seconds"] >= deep_work_min:
                deep_work_sec += s["duration_seconds"]
        calendar_stats["total_deep_work_seconds"] = min(deep_work_sec, non_meeting_sec)
        calendar_stats["total_shallow_work_seconds"] = max(0, non_meeting_sec - deep_work_sec)

        # Longest meeting-free block
        if sessions:
            sorted_sessions = sorted(sessions, key=lambda s: s["start"])
            max_free = 0
            for i in range(len(sorted_sessions)):
                if not any(m["title"] for m in calendar_stats["meetings"]):
                    break
                # Simple: find gaps between meetings
                if i == 0:
                    continue
                gap = (pd.Timestamp(sorted_sessions[i]["start"]) -
                       pd.Timestamp(sorted_sessions[i-1]["end"])).total_seconds()
                if gap > max_free:
                    max_free = gap
            calendar_stats["longest_meeting_free_block"] = max_free

    stats["calendar"] = calendar_stats

    return stats


def calc_comparison(current_stats: dict, prev_sessions_path: Path) -> list[dict]:
    """Compare current day stats with previous day."""
    if not prev_sessions_path.exists():
        return []
    with open(prev_sessions_path) as f:
        prev_sessions = json.load(f)
    if not prev_sessions:
        return []

    prev_sdf = pd.DataFrame(prev_sessions)
    prev_total = prev_sdf["duration_seconds"].sum()
    prev_focus = len(prev_sdf[prev_sdf["duration_seconds"] > 1500])

    cur = current_stats.get("overview", {})
    cur_total = cur.get("total_active_seconds", 0)
    cur_focus = cur.get("focus_session_count", 0)
    cur_switches = cur.get("switches_per_hour", 0)
    cur_idle = cur.get("idle_ratio", 0)

    # Previous switches/h and idle are not stored in sessions — approximate
    prev_switches_h = 0
    prev_idle = 0

    # Top project
    cur_proj = current_stats.get("projects", [{}])[0].get("project", "—") if current_stats.get("projects") else "—"
    prev_proj_group = prev_sdf.groupby("project")["duration_seconds"].sum()
    prev_top_proj = prev_proj_group.idxmax() if not prev_proj_group.empty else "—"

    def delta_str(cur_v: float, prev_v: float, unit: str = "", invert: bool = False) -> str:
        if prev_v == 0:
            return "—"
        pct = ((cur_v - prev_v) / prev_v) * 100
        arrow = "↑" if pct > 0 else "↓"
        qualifier = ""
        if invert:
            qualifier = " better" if pct < 0 else ""
        else:
            qualifier = " better" if pct > 0 else ""
        return f"{arrow} {abs(pct):.0f}%{qualifier}"

    return [
        {"metric": "Work time", "today": fmt_duration(cur_total), "yesterday": fmt_duration(prev_total), "delta": delta_str(cur_total, prev_total)},
        {"metric": "Focus sessions", "today": str(cur_focus), "yesterday": str(prev_focus), "delta": delta_str(cur_focus, prev_focus)},
        {"metric": "App switches/h", "today": str(cur_switches), "yesterday": str(prev_switches_h) if prev_switches_h else "—", "delta": delta_str(cur_switches, prev_switches_h, invert=True) if prev_switches_h else "—"},
        {"metric": "Idle ratio", "today": f"{cur_idle:.0%}", "yesterday": f"{prev_idle:.0%}" if prev_idle else "—", "delta": delta_str(cur_idle, prev_idle, invert=True) if prev_idle else "—"},
        {"metric": "Top project", "today": cur_proj, "yesterday": prev_top_proj, "delta": "—"},
    ]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def fmt_duration(seconds: float) -> str:
    """Format seconds to 'Xh XXmin' or 'XXmin'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    h, m = divmod(s // 60, 60)
    if h > 0:
        return f"{h}h {m:02d}min"
    return f"{m}min"


def intensity_bar(score: float, width: int = 10) -> str:
    """Create a visual bar from intensity score 0-10."""
    filled = round(score * width / 10)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def pct_str(val: float) -> str:
    return f"{val:.0%}" if val < 1 else "100%"


# ---------------------------------------------------------------------------
# Markdown Renderers
# ---------------------------------------------------------------------------


def render_daily_md(date: datetime, stats: dict, comparison: list[dict]) -> str:
    """Render a complete daily Markdown report."""
    wd = WEEKDAYS.get(date.weekday(), "")
    date_str = date.strftime("%d.%m.%Y")
    ov = stats.get("overview", {})

    lines = [f"# WorkTracker — {wd}, {date_str}", ""]

    # --- Overview ---
    lines.append("## Overview")
    lines.append(f"- Active work time: {fmt_duration(ov.get('total_active_seconds', 0))}")
    lines.append(f"- Time range: {ov.get('time_range_start', '?')} – {ov.get('time_range_end', '?')}")
    fc = ov.get('focus_session_count', 0)
    ft = fmt_duration(ov.get('focus_total_seconds', 0))
    lines.append(f"- Focus sessions (>25min): {fc} (total {ft})")
    lines.append(f"- App switches: {ov.get('app_switches', 0)} (avg {ov.get('switches_per_hour', 0)}/h)")
    lines.append(f"- Idle ratio: {ov.get('idle_ratio', 0):.0%}")
    lines.append(f"- Parallel media: {ov.get('media_ratio', 0):.0%} of work time")
    lines.append("")

    # --- Project Distribution ---
    lines.append("## Project Distribution")
    lines.append("| Project | Time | Share | Sessions | Avg Session | Intensity |")
    lines.append("|---------|------|--------|----------|-----------|------------|")
    for p in stats.get("projects", []):
        lines.append(
            f"| {p['project']} | {fmt_duration(p['total_seconds'])} | {pct_str(p['pct'])} "
            f"| {p['session_count']} | {fmt_duration(p['avg_duration'])} "
            f"| {intensity_bar(p['avg_intensity'])} |"
        )
    lines.append("")

    # --- App Usage ---
    lines.append("## App Usage")
    lines.append("| App | Time | Share | Top Project |")
    lines.append("|-----|------|--------|-------------|")
    for a in stats.get("apps", []):
        lines.append(f"| {a['app_name']} | {fmt_duration(a['total_seconds'])} | {pct_str(a['pct'])} | {a['top_project']} |")
    lines.append("")

    # --- Timeline ---
    lines.append("## Timeline")
    lines.append("| From | To | Duration | App | Context | URL/Branch | Project | Intensity |")
    lines.append("|-----|-----|-------|-----|---------|------------|---------|------------|")
    for s in stats.get("timeline", []):
        start_t = pd.Timestamp(s["start"]).strftime("%H:%M")
        end_t = pd.Timestamp(s["end"]).strftime("%H:%M")
        dur = fmt_duration(s["duration_seconds"])
        wt = _safe_str(s.get("window_title"))
        title_short = wt[:40] + "…" if len(wt) > 40 else wt
        # URL or git branch as extra context
        extra = ""
        if s.get("url"):
            try:
                domain = urlparse(s["url"]).netloc
                extra = domain if domain else ""
            except Exception:
                pass
        elif s.get("git_repo"):
            extra = f"{s['git_repo']}:{s.get('git_branch', '')}"
        lines.append(
            f"| {start_t} | {end_t} | {dur} | {s['app_name']} "
            f"| {title_short} | {extra} | {s['project']} | {intensity_bar(s['intensity_score'])} |"
        )
    lines.append("")

    # --- Parallel Activities ---
    media_entries = stats.get("parallel_media", [])
    if media_entries:
        lines.append("## Parallel Activities")
        for me in media_entries:
            start_t = pd.Timestamp(me["start"]).strftime("%H:%M")
            end_t = pd.Timestamp(me["end"]).strftime("%H:%M")
            m = me["media"]
            title = m.get("title", "")
            svc = m.get("service", m.get("app", ""))
            lines.append(f"- {start_t}–{end_t}: {svc} \"{title}\" playing during {me['app']}")
        lines.append("")

    # --- Input Analysis ---
    lines.append("## Input Analysis")
    inp = stats.get("input", {})
    hk = stats.get("hourly_keystrokes", {})
    if hk:
        peak_h = inp.get("peak_hour", 0)
        peak_val = hk.get(peak_h, 0)
        lines.append(f"- Keystroke peak: {peak_h}:00–{peak_h+1}:00 ({peak_val} keystrokes)")
        lines.append(f"- Most productive hour: {peak_h}:00")
    lines.append(f"- Total keystrokes: {inp.get('total_keystrokes', 0)}")
    lines.append(f"- Total clicks: {inp.get('total_clicks', 0)}")
    if hk:
        max_val = max(hk.values()) if hk.values() else 1
        lines.append("- Hourly intensity:")
        for h in sorted(hk.keys()):
            bar_len = round(hk[h] / max(max_val, 1) * 10)
            bar = "█" * bar_len + "░" * (10 - bar_len)
            lines.append(f"  {h:02d}: {bar} ({hk[h]})")
    lines.append("")

    # --- Clipboard-Transfers ---
    ct = stats.get("clipboard_transfers", [])
    if ct:
        lines.append("## Clipboard Transfers")
        lines.append("| Route | Count |")
        lines.append("|-------|--------|")
        for c in ct:
            lines.append(f"| {c['route']} | {c['count']}x |")
        lines.append("")

    # --- Detected Patterns ---
    patterns = stats.get("patterns", [])
    if patterns:
        lines.append("## Detected Patterns")
        for p in patterns:
            lines.append(f"- {p}")
        lines.append("")

    # --- URL-Domains ---
    url_domains = stats.get("url_domains", [])
    if url_domains:
        lines.append("## Browser Domains")
        lines.append("| Domain | Time |")
        lines.append("|--------|------|")
        for ud in url_domains[:15]:
            lines.append(f"| {ud['domain']} | {fmt_duration(ud['total_seconds'])} |")
        lines.append("")

    # --- Git Activity ---
    git = stats.get("git_activity", {})
    if git.get("total_commits", 0) > 0:
        lines.append("## Git Activity")
        lines.append(f"- Total commits: {git['total_commits']}")
        lines.append(f"- Active repos: {', '.join(git.get('active_repos', []))}")
        lines.append("")
        lines.append("| Zeit | Repo | Commit |")
        lines.append("|------|------|--------|")
        for c in git["commits"]:
            try:
                ts = pd.Timestamp(c["timestamp"]).strftime("%H:%M")
            except Exception:
                ts = "?"
            msg = c["message"][:50] + "…" if len(c["message"]) > 50 else c["message"]
            lines.append(f"| {ts} | {c['repo']} | {msg} |")
        lines.append("")

    # --- Calendar & Time Classification ---
    cal = stats.get("calendar", {})
    total_meeting = cal.get("total_meeting_seconds", 0)
    total_deep = cal.get("total_deep_work_seconds", 0)
    total_shallow = cal.get("total_shallow_work_seconds", 0)
    if total_meeting > 0 or cal.get("meetings"):
        total_classified = total_meeting + total_deep + total_shallow
        lines.append("## Calendar & Time Classification")
        if total_classified > 0:
            lines.append(f"- Meetings: {fmt_duration(total_meeting)} ({total_meeting / total_classified:.0%})")
            lines.append(f"- Deep Work: {fmt_duration(total_deep)} ({total_deep / total_classified:.0%})")
            lines.append(f"- Shallow Work: {fmt_duration(total_shallow)} ({total_shallow / total_classified:.0%})")
        longest_free = cal.get("longest_meeting_free_block", 0)
        if longest_free > 0:
            lines.append(f"- Longest meeting-free block: {fmt_duration(longest_free)}")
        lines.append("")
        meetings = cal.get("meetings", [])
        if meetings:
            lines.append("### Meetings")
            lines.append("| Title | Calendar | Attendees |")
            lines.append("|-------|----------|------------|")
            for m in meetings:
                lines.append(f"| {m['title']} | {m['calendar']} | {m['attendees']} |")
            lines.append("")

    # --- Previous Day Comparison ---
    if comparison:
        lines.append("## Previous Day Comparison")
        lines.append("| Metric | Today | Yesterday | Delta |")
        lines.append("|--------|-------|---------|-------|")
        for c in comparison:
            lines.append(f"| {c['metric']} | {c['today']} | {c['yesterday']} | {c['delta']} |")
        lines.append("")

    return "\n".join(lines)


def render_weekly_md(date: datetime, all_sessions: list[dict], daily_stats_list: list[tuple[datetime, dict]]) -> str:
    """Render a weekly Markdown report."""
    # Calculate week boundaries (ISO week, Monday start)
    iso_year, iso_week, _ = date.isocalendar()
    monday = datetime.fromisocalendar(iso_year, iso_week, 1)
    sunday = monday + timedelta(days=6)

    lines = [f"# WorkTracker — Week W{iso_week:02d}, {monday.strftime('%d.%m.')} – {sunday.strftime('%d.%m.%Y')}", ""]

    if not all_sessions:
        lines.append("*No data for this week.*")
        return "\n".join(lines)

    sdf = pd.DataFrame(all_sessions)
    sdf["start_dt"] = pd.to_datetime(sdf["start"], format="ISO8601")
    sdf["day_name"] = sdf["start_dt"].dt.weekday

    total_sec = sdf["duration_seconds"].sum()
    focus = sdf[sdf["duration_seconds"] > 1500]

    # --- Overview ---
    lines.append("## Overview")
    lines.append(f"- Active work time: {fmt_duration(total_sec)}")
    lines.append(f"- Days with activity: {sdf['start_dt'].dt.date.nunique()}")
    lines.append(f"- Focus sessions (>25min): {len(focus)} (total {fmt_duration(focus['duration_seconds'].sum())})")
    lines.append(f"- Avg work time/day: {fmt_duration(total_sec / max(1, sdf['start_dt'].dt.date.nunique()))}")
    lines.append("")

    # --- Daily Comparison ---
    lines.append("## Daily Comparison")
    lines.append("| Day | Work Time | Focus | Top Project | Intensity |")
    lines.append("|-----|-------------|-------|-------------|------------|")
    for dow in range(7):
        day_sessions = sdf[sdf["day_name"] == dow]
        if day_sessions.empty:
            continue
        day_total = day_sessions["duration_seconds"].sum()
        day_focus = len(day_sessions[day_sessions["duration_seconds"] > 1500])
        top_proj = day_sessions.groupby("project")["duration_seconds"].sum().idxmax() if not day_sessions.empty else "—"
        avg_int = day_sessions["intensity_score"].mean()
        lines.append(
            f"| {WEEKDAYS[dow][:2]} | {fmt_duration(day_total)} | {day_focus} "
            f"| {top_proj} | {intensity_bar(avg_int)} |"
        )
    lines.append("")

    # --- Weekly Project Distribution ---
    lines.append("## Weekly Project Distribution")
    lines.append("| Project | Time | Share | Sessions | Avg Session |")
    lines.append("|---------|------|--------|----------|-----------|")
    proj_group = sdf.groupby("project").agg(
        total_seconds=("duration_seconds", "sum"),
        session_count=("duration_seconds", "count"),
        avg_duration=("duration_seconds", "mean"),
    ).sort_values("total_seconds", ascending=False)
    for _, row in proj_group.iterrows():
        pct = row["total_seconds"] / total_sec
        lines.append(
            f"| {row.name} | {fmt_duration(row['total_seconds'])} | {pct_str(pct)} "
            f"| {int(row['session_count'])} | {fmt_duration(row['avg_duration'])} |"
        )
    lines.append("")

    # --- Trends ---
    lines.append("## Trends")
    day_totals = sdf.groupby(sdf["start_dt"].dt.date)["duration_seconds"].sum()
    if not day_totals.empty:
        best_day = day_totals.idxmax()
        worst_day = day_totals.idxmin()
        lines.append(f"- Most productive day: {WEEKDAYS.get(best_day.weekday(), '')} ({fmt_duration(day_totals[best_day])})")
        if best_day != worst_day:
            lines.append(f"- Least productive day: {WEEKDAYS.get(worst_day.weekday(), '')} ({fmt_duration(day_totals[worst_day])})")
    lines.append("")

    # --- Weekly Git Activity ---
    git_repos_week: dict[str, int] = {}
    for s in all_sessions:
        repo = s.get("git_repo", "")
        if repo:
            git_repos_week[repo] = git_repos_week.get(repo, 0) + 1
    if git_repos_week:
        lines.append("## Git Activity")
        lines.append("| Repo | Sessions |")
        lines.append("|------|----------|")
        for repo, count in sorted(git_repos_week.items(), key=lambda x: -x[1]):
            lines.append(f"| {repo} | {count} |")
        lines.append("")

    # --- Browser Top-Domains Woche ---
    url_domains_week: dict[str, int] = {}
    for s in all_sessions:
        url = s.get("url", "")
        if url:
            try:
                domain = urlparse(url).netloc
                if domain:
                    url_domains_week[domain] = url_domains_week.get(domain, 0) + s.get("duration_seconds", 0)
            except Exception:
                pass
    if url_domains_week:
        lines.append("## Top Browser Domains")
        lines.append("| Domain | Time |")
        lines.append("|--------|------|")
        for domain, sec in sorted(url_domains_week.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"| {domain} | {fmt_duration(sec)} |")
        lines.append("")

    # --- Previous Week Comparison ---
    prev_monday = monday - timedelta(weeks=1)
    prev_sunday = prev_monday + timedelta(days=6)
    prev_sessions = _load_sessions_range(prev_monday, prev_sunday)
    if prev_sessions:
        prev_sdf = pd.DataFrame(prev_sessions)
        prev_total = prev_sdf["duration_seconds"].sum()
        prev_focus = len(prev_sdf[prev_sdf["duration_seconds"] > 1500])
        delta_time = ((total_sec - prev_total) / prev_total * 100) if prev_total else 0
        delta_focus = ((len(focus) - prev_focus) / prev_focus * 100) if prev_focus else 0

        lines.append("## Previous Week Comparison")
        lines.append("| Metric | This Week | Last Week | Delta |")
        lines.append("|--------|-----------|-----------|-------|")
        lines.append(f"| Work time | {fmt_duration(total_sec)} | {fmt_duration(prev_total)} | {'↑' if delta_time >= 0 else '↓'} {abs(delta_time):.0f}% |")
        lines.append(f"| Focus sessions | {len(focus)} | {prev_focus} | {'↑' if delta_focus >= 0 else '↓'} {abs(delta_focus):.0f}% |")
        lines.append("")

    return "\n".join(lines)


def render_monthly_md(date: datetime, all_sessions: list[dict]) -> str:
    """Render a monthly Markdown report."""
    month_name = MONTH_NAMES.get(date.month, "")
    year = date.year

    lines = [f"# WorkTracker — {month_name} {year}", ""]

    if not all_sessions:
        lines.append("*No data for this month.*")
        return "\n".join(lines)

    sdf = pd.DataFrame(all_sessions)
    sdf["start_dt"] = pd.to_datetime(sdf["start"], format="ISO8601")
    total_sec = sdf["duration_seconds"].sum()
    focus = sdf[sdf["duration_seconds"] > 1500]
    active_days = sdf["start_dt"].dt.date.nunique()

    # --- Overview ---
    lines.append("## Overview")
    lines.append(f"- Active work time: {fmt_duration(total_sec)}")
    lines.append(f"- Days with activity: {active_days}")
    lines.append(f"- Focus sessions (>25min): {len(focus)} (total {fmt_duration(focus['duration_seconds'].sum())})")
    lines.append(f"- Avg work time/day: {fmt_duration(total_sec / max(1, active_days))}")
    lines.append("")

    # --- Weekly Comparison ---
    sdf["iso_week"] = sdf["start_dt"].dt.isocalendar().week.astype(int)
    lines.append("## Weekly Comparison")
    lines.append("| Week | Work Time | Focus | Top Project | Intensity |")
    lines.append("|-------|-------------|-------|-------------|------------|")
    for week_num, wdf in sdf.groupby("iso_week"):
        w_total = wdf["duration_seconds"].sum()
        w_focus = len(wdf[wdf["duration_seconds"] > 1500])
        w_top = wdf.groupby("project")["duration_seconds"].sum().idxmax() if not wdf.empty else "—"
        w_int = wdf["intensity_score"].mean()
        lines.append(f"| W{week_num:02d} | {fmt_duration(w_total)} | {w_focus} | {w_top} | {intensity_bar(w_int)} |")
    lines.append("")

    # --- Monthly Project Distribution ---
    lines.append("## Monthly Project Distribution")
    lines.append("| Project | Time | Share | Sessions |")
    lines.append("|---------|------|--------|----------|")
    proj_group = sdf.groupby("project").agg(
        total_seconds=("duration_seconds", "sum"),
        session_count=("duration_seconds", "count"),
    ).sort_values("total_seconds", ascending=False)
    for _, row in proj_group.iterrows():
        pct = row["total_seconds"] / total_sec
        lines.append(f"| {row.name} | {fmt_duration(row['total_seconds'])} | {pct_str(pct)} | {int(row['session_count'])} |")
    lines.append("")

    # --- Long-term Trends ---
    lines.append("## Long-term Trends")
    daily_totals = sdf.groupby(sdf["start_dt"].dt.date)["duration_seconds"].sum()
    if len(daily_totals) > 1:
        first_half = daily_totals.iloc[:len(daily_totals)//2].mean()
        second_half = daily_totals.iloc[len(daily_totals)//2:].mean()
        trend = "increasing" if second_half > first_half else "decreasing"
        lines.append(f"- Work time trend: {trend} (1st half avg {fmt_duration(first_half)}/day → 2nd half avg {fmt_duration(second_half)}/day)")
    lines.append("")

    # --- Monthly Git Activity ---
    git_repos_month: dict[str, int] = {}
    for s in all_sessions:
        repo = s.get("git_repo", "")
        if repo:
            git_repos_month[repo] = git_repos_month.get(repo, 0) + 1
    if git_repos_month:
        lines.append("## Git Activity")
        lines.append("| Repo | Sessions |")
        lines.append("|------|----------|")
        for repo, count in sorted(git_repos_month.items(), key=lambda x: -x[1]):
            lines.append(f"| {repo} | {count} |")
        lines.append("")

    # --- Previous Month Comparison ---
    if date.month == 1:
        prev_month_date = date.replace(year=date.year - 1, month=12, day=1)
    else:
        prev_month_date = date.replace(month=date.month - 1, day=1)
    prev_month_end = date.replace(day=1) - timedelta(days=1)
    prev_sessions = _load_sessions_range(prev_month_date, prev_month_end)
    if prev_sessions:
        prev_sdf = pd.DataFrame(prev_sessions)
        prev_total = prev_sdf["duration_seconds"].sum()
        delta = ((total_sec - prev_total) / prev_total * 100) if prev_total else 0
        prev_name = MONTH_NAMES.get(prev_month_date.month, "")
        lines.append("## Previous Month Comparison")
        lines.append("| Metric | This Month | Last Month | Delta |")
        lines.append("|--------|------------|------------|-------|")
        lines.append(f"| Work time | {fmt_duration(total_sec)} | {fmt_duration(prev_total)} | {'↑' if delta >= 0 else '↓'} {abs(delta):.0f}% |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Session I/O helpers
# ---------------------------------------------------------------------------


def _load_sessions_range(start: datetime, end: datetime) -> list[dict]:
    """Load all session files in a date range."""
    all_sessions = []
    d = start
    while d <= end:
        path = SESSIONS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if path.exists():
            with open(path) as f:
                all_sessions.extend(json.load(f))
        d += timedelta(days=1)
    return all_sessions


# ---------------------------------------------------------------------------
# Mode Runners
# ---------------------------------------------------------------------------


def run_daily(date: datetime) -> None:
    log.info("Daily report for %s", date.strftime("%Y-%m-%d"))

    df = load_snapshots(date)
    if df.empty:
        log.warning("No snapshots — aborting.")
        return

    # ── Midnight continuity: load tail of previous day (≥23:55) ──────
    prev_date = date - timedelta(days=1)
    prev_df = load_snapshots(prev_date)
    if not prev_df.empty:
        prev_tail = prev_df[
            (prev_df["ts"].dt.hour == 23) & (prev_df["ts"].dt.minute >= 55)
        ]
        if not prev_tail.empty:
            log.info(
                "Midnight merge: %d snapshots loaded from previous day", len(prev_tail),
            )
            df = pd.concat([prev_tail, df], ignore_index=True).sort_values("ts").reset_index(drop=True)

    df = flatten_df(df)
    cfg = load_config()

    # Detect sessions
    sessions = detect_sessions(df, cfg)

    # ── Clip midnight-carryover sessions to target date ──────────────
    midnight = pd.Timestamp(date.strftime("%Y-%m-%d"), tz=TZ)
    clipped = []
    for s in sessions:
        end_ts = pd.Timestamp(s["end"])
        if end_ts < midnight:
            continue  # session belongs entirely to previous day
        start_ts = pd.Timestamp(s["start"])
        if start_ts < midnight:
            # Clip start to midnight, recalculate duration
            s["start"] = midnight.isoformat()
            s["duration_seconds"] = (end_ts - midnight).total_seconds()
        clipped.append(s)
    sessions = clipped
    log.info("%d Sessions erkannt", len(sessions))

    # Save sessions
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    sessions_path = SESSIONS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    with open(sessions_path, "w") as f:
        json.dump(sessions, f, indent=2, ensure_ascii=False)
    log.info("Sessions gespeichert: %s", sessions_path)

    # Calculate stats
    try:
        stats = calc_daily_stats(df, sessions, cfg)
    except Exception as e:
        log.error("Error in statistics calculation: %s", e, exc_info=True)
        stats = {"overview": {}, "projects": [], "apps": [], "timeline": sessions}

    # Comparison with previous day
    prev_date = date - timedelta(days=1)
    prev_sessions_path = SESSIONS_DIR / f"{prev_date.strftime('%Y-%m-%d')}.json"
    try:
        comparison = calc_comparison(stats, prev_sessions_path)
    except Exception as e:
        log.error("Error in comparison: %s", e, exc_info=True)
        comparison = []

    # Render Markdown
    md = render_daily_md(date, stats, comparison)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    md_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
    with open(md_path, "w") as f:
        f.write(md)
    log.info("Daily-Report geschrieben: %s", md_path)
    print(f"✓ Daily-Report: {md_path}")

    # Auto-suggest patterns for unmatched sessions
    try:
        suggest_patterns(sessions, date)
    except Exception as e:
        log.warning("Pattern-Suggestion fehlgeschlagen: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Pattern Suggestion (dynamic learning)
# ---------------------------------------------------------------------------


def suggest_patterns(sessions: list[dict], date: datetime) -> None:
    """Analyze 'Other' sessions and write suggestions to learned_patterns.yaml."""
    sonstiges = [s for s in sessions if s.get("project", "").startswith("Other")]
    if not sonstiges:
        log.info("No 'Other' sessions — no suggestions needed.")
        return

    # Group by extracted keyword / domain
    from urllib.parse import urlparse as _urlparse

    groups: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "total_seconds": 0.0, "titles": set(), "urls": set()}
    )

    for s in sonstiges:
        title = _safe_str(s.get("window_title"))
        url = _safe_str(s.get("url"))
        dur = s.get("duration_seconds", 0)

        # Extract domain from URL if available
        key = None
        if url:
            try:
                domain = _urlparse(url).netloc.replace("www.", "")
                if domain:
                    key = domain
            except Exception:
                pass

        # Fall back to significant title words
        if not key and title:
            # Take the first meaningful word (>4 chars, not common)
            stopwords = {"the", "and", "for", "with", "this", "from", "that", "oder",
                         "und", "der", "die", "das", "ein", "eine"}
            words = [w for w in title.split() if len(w) > 4 and w.lower() not in stopwords]
            if words:
                key = words[0].lower().strip("…·|—-:,.")

        if not key:
            continue

        groups[key]["count"] += 1
        groups[key]["total_seconds"] += dur
        if title:
            groups[key]["titles"].add(title[:60])
        if url:
            groups[key]["urls"].add(url)

    # Only keep groups with significant time (>5 minutes)
    significant = {k: v for k, v in groups.items() if v["total_seconds"] >= 300}
    if not significant:
        log.info("No significant 'Other' groups found.")
        return

    # Build suggestion YAML
    learned_path = PATTERNS_PATH.parent / "learned_patterns.yaml"
    existing: dict = {}
    if learned_path.exists():
        with open(learned_path) as f:
            existing = yaml.safe_load(f) or {}

    projects = existing.setdefault("projects", {})
    date_str = date.strftime("%Y-%m-%d")

    for key, info in significant.items():
        # Create project name from key
        proj_name = key.replace(".", " ").title()
        if proj_name in projects:
            # Update existing entry
            projects[proj_name]["_total_time_seconds"] = round(
                projects[proj_name].get("_total_time_seconds", 0) + info["total_seconds"]
            )
            projects[proj_name]["_last_seen"] = date_str
            continue

        entry: dict[str, Any] = {
            "patterns": [f"*{key}*"],
            "category": "Uncategorized",
            "_auto_generated": True,
            "_total_time_seconds": round(info["total_seconds"]),
            "_first_seen": date_str,
            "_sample_titles": sorted(info["titles"])[:3],
        }
        if info["urls"]:
            entry["url_patterns"] = [f"*{key}*"]
        projects[proj_name] = entry

    with open(learned_path, "w") as f:
        yaml.dump(existing, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    log.info("Pattern-Suggestions geschrieben: %s (%d Gruppen)", learned_path, len(significant))
    print(f"✓ Suggestions: {learned_path}")


def run_weekly(date: datetime) -> None:
    iso_year, iso_week, _ = date.isocalendar()
    monday = datetime.fromisocalendar(iso_year, iso_week, 1)
    sunday = monday + timedelta(days=6)
    log.info("Weekly-Report W%02d (%s – %s)", iso_week, monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"))

    # First ensure daily sessions exist for each day
    d = monday
    while d <= min(sunday, datetime.now()):
        sess_path = SESSIONS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if not sess_path.exists():
            snap_path = SNAPSHOTS_DIR / f"{d.strftime('%Y-%m-%d')}.jsonl"
            if snap_path.exists():
                log.info("Generating missing sessions for %s", d.strftime("%Y-%m-%d"))
                run_daily(d)
        d += timedelta(days=1)

    # Load all sessions for the week
    all_sessions = _load_sessions_range(monday, sunday)
    if not all_sessions:
        log.warning("No sessions for week W%02d", iso_week)

    md = render_weekly_md(date, all_sessions, [])
    WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
    md_path = WEEKLY_DIR / f"{iso_year}-W{iso_week:02d}.md"
    with open(md_path, "w") as f:
        f.write(md)
    log.info("Weekly report written: %s", md_path)
    print(f"✓ Weekly-Report: {md_path}")


def run_monthly(date: datetime) -> None:
    year, month = date.year, date.month
    first_day = date.replace(day=1)
    if month == 12:
        last_day = date.replace(year=year + 1, month=1, day=1) - timedelta(days=1)
    else:
        last_day = date.replace(month=month + 1, day=1) - timedelta(days=1)
    # Don't go past today
    last_day = min(last_day, datetime.now())

    log.info("Monthly report %s %d", MONTH_NAMES[month], year)

    # Ensure daily sessions exist
    d = first_day
    while d <= last_day:
        sess_path = SESSIONS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if not sess_path.exists():
            snap_path = SNAPSHOTS_DIR / f"{d.strftime('%Y-%m-%d')}.jsonl"
            if snap_path.exists():
                log.info("Generating missing sessions for %s", d.strftime("%Y-%m-%d"))
                run_daily(d)
        d += timedelta(days=1)

    all_sessions = _load_sessions_range(first_day, last_day)
    if not all_sessions:
        log.warning("No sessions for %s %d", MONTH_NAMES[month], year)

    md = render_monthly_md(date, all_sessions)
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    md_path = MONTHLY_DIR / f"{year}-{month:02d}.md"
    with open(md_path, "w") as f:
        f.write(md)
    log.info("Monthly report written: %s", md_path)
    print(f"✓ Monthly-Report: {md_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="WorkTracker Aggregator")
    parser.add_argument("--mode", choices=["daily", "weekly", "monthly"], required=True)
    parser.add_argument("--date", help="Datum im Format YYYY-MM-DD (Default: heute)", default=None)
    args = parser.parse_args()

    if args.date:
        target = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target = datetime.now()

    if args.mode == "daily":
        run_daily(target)
    elif args.mode == "weekly":
        run_weekly(target)
    elif args.mode == "monthly":
        run_monthly(target)


if __name__ == "__main__":
    main()
