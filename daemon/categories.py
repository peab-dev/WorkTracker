#!/usr/bin/env python3
"""WorkTracker Categories — shared category pool for apps, web URLs and topics.

Loads the central pool definition (categories.default.yaml + user overlay
categories.yaml) and provides classification/canonicalization helpers used by
the aggregator and the web dashboard:

  - canonical(name)              legacy/alias name → canonical activity name
  - color(name)                  pool color, stable hash fallback
  - activity_names()             ordered canonical activity categories
  - classify_domain(url)         (activity, subcategory) — DOMAIN_CATEGORIES
                                 seed overlaid with YAML domain_categories
  - classify_app(name, cats)     app name → tool category (fnmatch)
  - app_subcategory(...)         tool category + app/project → subcategory
  - tool_activity(tool_cat)      tool category → activity (bridge)
  - derive_activity_category(s)  session → canonical activity (topic
                                 inheritance: project → web → tool bridge)
  - validate_project_categories  unknown project category values

Only stdlib + yaml; importable by daemon and dashboard alike.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

DEFAULT_FILE = Path(__file__).parent / "categories.default.yaml"
USER_FILE = Path(__file__).parent / "categories.yaml"

_USER_STUB = """# WorkTracker — User category pool (gitignored, overlays categories.default.yaml)
#
# Add custom categories, colors, and aliases here. See
# categories.default.yaml for the schema. Example:
#   activity_categories:
#     Hobby: { color: "#aabbcc", aliases: [] }
#   domain_categories:
#     example.com: [News, Magazines]

activity_categories: {}
domain_categories: {}
"""

_INVISIBLE_RE = re.compile(
    r"[\u200e\u200f\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064"
    r"\ufeff\u00ad\u034f\u061c\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


def _clean_name(name: str) -> str:
    """Strip invisible unicode chars (LRM, soft-hyphen, ZWS, etc.)."""
    return _INVISIBLE_RE.sub("", name or "")


def _ensure_user_file() -> None:
    if not USER_FILE.exists():
        try:
            USER_FILE.write_text(_USER_STUB)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Pool loading (default + user overlay, mtime-cached)
# ---------------------------------------------------------------------------

_cache: dict = {"mtimes": None, "pool": None}


def _read_yaml(path: Path) -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def load_pool() -> dict:
    """Merged pool: {activity, tool, domains, alias_map}.

    activity:  {canonical_name: {color, aliases, subcategories}}
    tool:      {tool_name: {color, activity, app_subcategories}}
    domains:   {domain: (activity, subcategory)} — YAML overrides only;
               the built-in seed lives in web_categories.DOMAIN_CATEGORIES.
    alias_map: {alias_lower: canonical_name}
    """
    _ensure_user_file()
    mtimes = tuple(
        p.stat().st_mtime if p.exists() else 0 for p in (DEFAULT_FILE, USER_FILE)
    )
    if _cache["pool"] is not None and _cache["mtimes"] == mtimes:
        return _cache["pool"]

    activity: dict = {}
    tool: dict = {}
    domains: dict = {}
    for path in (DEFAULT_FILE, USER_FILE):
        data = _read_yaml(path)
        for name, info in (data.get("activity_categories") or {}).items():
            activity[str(name)] = dict(info or {})
        for name, info in (data.get("tool_categories") or {}).items():
            tool[str(name)] = dict(info or {})
        for dom, pair in (data.get("domain_categories") or {}).items():
            if isinstance(pair, (list, tuple)) and len(pair) >= 1:
                main = str(pair[0])
                sub = str(pair[1]) if len(pair) > 1 else ""
                domains[str(dom).lower()] = (main, sub)

    alias_map: dict[str, str] = {}
    for name, info in activity.items():
        alias_map[name.lower()] = name
        for alias in info.get("aliases") or []:
            alias_map[str(alias).lower()] = name

    pool = {"activity": activity, "tool": tool, "domains": domains,
            "alias_map": alias_map}
    _cache["mtimes"] = mtimes
    _cache["pool"] = pool
    return pool


# ---------------------------------------------------------------------------
# Canonicalization / colors
# ---------------------------------------------------------------------------


def canonical(name: str) -> str:
    """Map a (legacy) category name to its canonical pool name.

    Unknown names pass through unchanged — never dropped.
    """
    name = _clean_name(str(name or "")).strip()
    if not name:
        return ""
    return load_pool()["alias_map"].get(name.lower(), name)


def color(name: str) -> str:
    """Pool color for a category (activity first, then tool); hash fallback."""
    cat = canonical(name)
    if not cat:
        return "#8a95a3"
    pool = load_pool()
    info = pool["activity"].get(cat) or pool["tool"].get(cat)
    if info and info.get("color"):
        return str(info["color"])
    h = 0
    for ch in cat:
        h = (h * 31 + ord(ch)) % 360
    return f"hsl({h}, 65%, 62%)"


def activity_names() -> list[str]:
    """Canonical activity categories in pool order ('Other' last)."""
    names = list(load_pool()["activity"].keys())
    if "Other" in names:
        names.remove("Other")
        names.append("Other")
    return names


def alias_map() -> dict[str, str]:
    """{alias_lower: canonical} — for the dashboard JS."""
    return dict(load_pool()["alias_map"])


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_domain(url: str) -> tuple[str, str]:
    """Classify a URL into (activity_category, subcategory).

    YAML domain_categories overlay the built-in DOMAIN_CATEGORIES seed;
    matching strategy mirrors the original web_categories.classify_url.
    """
    import web_categories  # lazy: avoids circular import at module load

    overrides = load_pool()["domains"]

    def lookup(dom: str):
        return overrides.get(dom) or web_categories.DOMAIN_CATEGORIES.get(dom)

    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ("Other", "Uncategorized")
    if not netloc:
        return ("Other", "Uncategorized")
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if ":" in netloc:
        netloc = netloc.split(":")[0]

    if netloc in ("localhost", "127.0.0.1", "0.0.0.0", "[::]", "[::1]"):
        return ("Development", "Local")

    result = lookup(netloc)
    if result:
        return (canonical(result[0]), result[1])

    # Parent-domain fallback: strip subdomains one level at a time
    parts = netloc.split(".")
    while len(parts) > 2:
        parts = parts[1:]
        result = lookup(".".join(parts))
        if result:
            return (canonical(result[0]), result[1])

    # Two-part TLDs (co.uk, gv.at, or.at, …): e.g. news.bbc.co.uk → bbc.co.uk
    if len(parts) >= 3 and parts[-2] in ("co", "or", "gv", "ac", "org"):
        result = lookup(".".join(parts[-3:]))
        if result:
            return (canonical(result[0]), result[1])

    return ("Other", "Uncategorized")


def classify_app(app_name: str, app_categories: dict) -> str:
    """Match an app name against app_categories patterns → tool category.

    Supports both flat (list) and structured (dict with 'apps') formats.
    Single implementation for aggregator and dashboard.
    """
    if not app_name:
        return "Other"
    app_lower = _clean_name(app_name).lower()
    for category, cat_info in (app_categories or {}).items():
        if category == "Other":
            continue
        apps_list = cat_info if isinstance(cat_info, list) else cat_info.get("apps", [])
        for pattern in apps_list:
            if fnmatch.fnmatch(app_lower, str(pattern).lower()):
                return category
    return "Other"


def app_subcategory(tool_cat: str, app_name: str, project_category: str) -> str:
    """Subcategory for a session.

    Browser: the canonical *project* activity category (News, AI, …).
    Other tools: app-name lookup in the pool's app_subcategories map.
    """
    if tool_cat == "Browser":
        return canonical(project_category) or "Other"
    info = load_pool()["tool"].get(tool_cat) or {}
    sub_map = info.get("app_subcategories") or {}
    if sub_map and app_name:
        app_lower = _clean_name(app_name).lower()
        for pattern, sub in sub_map.items():
            if fnmatch.fnmatch(app_lower, str(pattern).lower()):
                return str(sub)
    return ""


def tool_activity(tool_cat: str) -> str:
    """Tool category → activity category bridge ('' if none, e.g. Browser)."""
    info = load_pool()["tool"].get(tool_cat) or {}
    return str(info.get("activity") or "")


def derive_activity_category(session: dict) -> str:
    """Canonical activity category for a session (and thus its topic).

    Priority: project category → web category → tool→activity bridge → Other.
    """
    cat = canonical(session.get("category") or "")
    if cat and cat != "Other":
        return cat
    web = canonical(session.get("web_category") or "")
    if web and web != "Other":
        return web
    return tool_activity(session.get("app_category") or "") or "Other"


def validate_project_categories(projects: dict) -> list[tuple[str, str]]:
    """Return [(project, category)] for category values unknown to the pool."""
    known = set(load_pool()["alias_map"].keys())
    unknown = []
    for proj, info in (projects or {}).items():
        if not isinstance(info, dict):
            continue
        cat = str(info.get("category") or "").strip()
        if cat and cat.lower() not in known:
            unknown.append((proj, cat))
    return unknown
