"""Topic extraction for WorkTracker sessions via a LOCAL LLM.

Sends rich session context (app name, window title, url host, filename,
project, duration, ambient window titles, clipboard text samples, git
repo/branch, calendar event, activity counts) to a local OpenAI-compatible
chat completions endpoint (LM Studio, Ollama, etc.) and writes a 2–20
word German topic string back into each session.

Because the brief contains clipboard samples and ambient window titles,
the configured ``topic_llm.endpoint`` MUST stay local (e.g. localhost).
Do not point this at a remote endpoint — the module trusts the endpoint
with private data that would never otherwise leave the machine.

Failure modes (timeout, bad JSON, endpoint down) are swallowed — the
aggregator keeps working with empty topics.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable
from urllib import request as _urlrequest
from urllib.error import URLError
from urllib.parse import urlparse

log = logging.getLogger("worktracker.topic_extractor")

_SYSTEM_PROMPT = (
    "Du extrahierst Themen aus Arbeits-Sessions. Eingabe: JSON-Array mit\n"
    "{idx, app, title, project, duration_min, optional host, filename,\n"
    "ambient_titles, clip_samples, git_repo, git_branch, calendar_event}.\n\n"
    "Antworte NUR mit einem JSON-Array, ein Objekt pro Eingabe-Session, idx\n"
    "unverändert übernehmen:\n"
    '[{"idx":<n>,"topic":"...","topic_long":"..."}]\n\n'
    "topic — 2 bis 20 deutsche Wörter. Beschreibt KONKRET WAS gemacht wurde\n"
    "(Datei, Repo, Feature, Inhalt, Recherche-Thema). MIN 2 Wörter sonst leer.\n"
    "KEINE App-Namen als eigenständige Wörter — verboten: Claude, ChatGPT,\n"
    "Gemini, WebStorm, PyCharm, Cursor, VS Code, Xcode, Safari, Chrome,\n"
    "Firefox, Arc, Terminal, iTerm, Finder, Mail, Slack, Discord, LM Studio,\n"
    "Photopea, Spotify.\n\n"
    "topic_long — 1 bis 2 deutsche Sätze, max 250 Zeichen, etwas mehr Kontext\n"
    "als topic. App-Namen hier ERLAUBT.\n\n"
    "Wenn title nur dem App-Namen entspricht (z.B. title=\"Claude\"): leite das\n"
    "Thema aus ambient_titles, clip_samples, git_repo und filename ab — sie\n"
    "zeigen den echten Arbeitskontext.\n\n"
    "Variiere Formulierungen — keine identischen Topics für ähnliche Sessions.\n\n"
    "Nur das JSON-Array. Keine Prosa, keine Code-Fences, keine Kommentare."
)


def _is_too_thin(brief: dict) -> bool:
    """True when there's basically nothing content-ish for the LLM to work with.

    A title that is *only* the app name (e.g. Claude Desktop's literal
    ``"Claude"``) does NOT count as content — but such a session is kept
    as long as *any* other context field (ambient windows, clipboard
    samples, git repo, calendar event, filename, host) has content.
    """
    def _nonempty(v) -> bool:
        if v is None:
            return False
        if isinstance(v, str):
            return bool(v.strip())
        if isinstance(v, (list, tuple, dict)):
            return len(v) > 0
        return bool(v)

    # Title only counts as "content" if it's not just the app name.
    title = str(brief.get("title") or "").strip().lower()
    app = str(brief.get("app") or "").strip().lower()
    title_has_content = bool(title) and title != app

    return not (
        title_has_content
        or any(
            _nonempty(brief.get(k))
            for k in (
                "filename", "host",
                "git_repo", "calendar_event",
                "ambient_titles", "clip_samples",
            )
        )
    )


def _host(url: str) -> str:
    if not url:
        return ""
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _filename(title: str) -> str:
    if not title:
        return ""
    m = re.search(r"[\w.-]+\.\w{1,5}", title)
    return m.group(0) if m else ""


def _strip_suffixes(title: str, suffixes: Iterable[str]) -> str:
    for s in suffixes or ():
        if title.endswith(s):
            return title[: -len(s)].strip()
    return title


def _clean_str(v: Any) -> str:
    """Coerce any value (including pandas NaN floats) to a stripped string."""
    if v is None:
        return ""
    if isinstance(v, float):
        # pandas NaN / missing-field sentinels
        if v != v:  # NaN
            return ""
        return str(v).strip()
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _dedupe_truncate(items: Iterable[str], max_items: int, max_chars: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items or ():
        if not it:
            continue
        s = str(it).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:max_chars])
        if len(out) >= max_items:
            break
    return out


def _session_brief(session: dict, title_suffixes: Iterable[str]) -> dict:
    raw_title = _clean_str(session.get("window_title"))
    semantic_title = _strip_suffixes(raw_title, title_suffixes)

    ambient_raw = session.get("_ambient_titles")
    if not isinstance(ambient_raw, (list, tuple)):
        ambient_raw = []
    ambient = _dedupe_truncate(
        (_strip_suffixes(_clean_str(t), title_suffixes) for t in ambient_raw),
        max_items=5, max_chars=80,
    )

    clip_raw = session.get("_clip_text_samples")
    if not isinstance(clip_raw, (list, tuple)):
        clip_raw = []
    clip_samples = _dedupe_truncate(
        (_clean_str(c) for c in clip_raw),
        max_items=2, max_chars=100,
    )

    def _safe_int(v: Any) -> int:
        try:
            if v is None or (isinstance(v, float) and v != v):
                return 0
            return int(v)
        except (TypeError, ValueError):
            return 0

    brief: dict[str, Any] = {
        "app": _clean_str(session.get("app_name")),
        "title": semantic_title[:120],
        "host": _host(_clean_str(session.get("url"))),
        "filename": _filename(raw_title),
        "project": _clean_str(session.get("project")),
        "duration_min": round(_safe_int(session.get("duration_seconds")) / 60),
    }
    if ambient:
        brief["ambient_titles"] = ambient
    if clip_samples:
        brief["clip_samples"] = clip_samples
    git_repo = _clean_str(session.get("git_repo"))
    git_branch = _clean_str(session.get("git_branch"))
    if git_repo:
        brief["git_repo"] = git_repo
    if git_branch:
        brief["git_branch"] = git_branch
    cal_event = _clean_str(session.get("calendar_event"))
    if cal_event:
        brief["calendar_event"] = cal_event
    return brief


def _post_json(endpoint: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = _urlrequest.Request(
        endpoint,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urlrequest.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def _parse_indexed_topics(text: str, expected: int) -> dict[int, dict[str, str]]:
    """Extract a dict {idx: {topic, topic_long}} from model output.

    Accepts three response shapes (all produced in the wild by various
    LM Studio / llama.cpp versions):

    1. ``{"results": [{"idx":0,"topic":"…","topic_long":"…"}, …]}`` — the
       new strict json_schema form (top-level object wrapper required by
       OpenAI structured-output strict mode).
    2. ``[{"idx":0,"topic":"…","topic_long":"…"}, …]`` — bare array,
       legacy / non-strict mode.
    3. ``[<topic-string>, …]`` — positional string fallback for very old
       responses.

    Code-fence wrappers (``` ```json ```) are stripped first.
    """
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    data = None
    try:
        data = json.loads(text)
    except Exception:
        # Last-ditch: pull out the first balanced array or object.
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None

    # Unwrap {results: [...]} into the bare array form.
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        data = data["results"]

    if not isinstance(data, list):
        return {}

    out: dict[int, dict[str, str]] = {}
    for i, item in enumerate(data):
        if isinstance(item, dict):
            idx = item.get("idx")
            topic = item.get("topic", "")
            topic_long = item.get("topic_long", "")
            if isinstance(idx, int) and 0 <= idx < expected:
                out[idx] = {
                    "topic": str(topic or "").strip(),
                    "topic_long": str(topic_long or "").strip(),
                }
        elif isinstance(item, str) and i < expected:
            # Positional fallback (legacy single-string responses)
            out[i] = {"topic": item.strip(), "topic_long": ""}
    return out


def extract_topics(sessions: list[dict], cfg: dict,
                    title_suffixes: "list[str] | tuple[str, ...]" = (),
                    progress: "callable | None" = None) -> int:
    """Annotate sessions in place with a ``topic`` field. Returns count set.

    Silent on all failures — if anything goes wrong, sessions keep
    ``topic == ""``.

    If *progress* is given, it's called after each batch with
    ``progress(done_batches, total_batches, topics_set_so_far)``.
    """
    topic_cfg = (cfg or {}).get("aggregator", {}).get("topic_llm", {}) or {}
    if not topic_cfg.get("enabled"):
        return 0

    endpoint = topic_cfg.get("endpoint") or ""
    if not endpoint:
        return 0
    model = topic_cfg.get("model", "local-model")
    timeout = float(topic_cfg.get("timeout_seconds", 8))
    batch_size = int(topic_cfg.get("batch_size", 10))
    max_sessions = int(topic_cfg.get("max_sessions_per_day", 200))
    min_dur = int(topic_cfg.get("min_session_seconds", 60))

    # Pre-filter: drop sessions that already have a topic or are too thin
    raw_candidates = [
        s for s in sessions
        if int(s.get("duration_seconds", 0)) >= min_dur
        and not str(s.get("topic") or "").strip()
    ]

    candidates = []
    for s in raw_candidates:
        brief = _session_brief(s, title_suffixes)
        if _is_too_thin(brief):
            continue
        candidates.append((s, brief))
        if len(candidates) >= max_sessions:
            break

    if not candidates:
        if progress:
            try:
                progress(0, 0, 0)
            except Exception:
                pass
        return 0

    total_batches = (len(candidates) + batch_size - 1) // batch_size
    set_count = 0
    for chunk_start in range(0, len(candidates), batch_size):
        batch_idx = chunk_start // batch_size + 1
        if progress:
            try:
                progress(batch_idx, total_batches, set_count)
            except Exception:
                pass
        batch = candidates[chunk_start: chunk_start + batch_size]
        # Attach idx so the model can't silently reorder
        briefs_with_idx = [
            {"idx": i, **brief} for i, (_s, brief) in enumerate(batch)
        ]
        # Schema is described in the system prompt — user message is just data.
        user_msg = json.dumps(briefs_with_idx, ensure_ascii=False)
        payload = {
            "model": model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
        }
        try:
            resp = _post_json(endpoint, payload, timeout)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            log.warning("topic_llm request failed: %s", e)
            continue
        except Exception as e:  # pragma: no cover — defensive
            log.warning("topic_llm unexpected error: %s", e)
            continue

        try:
            content = resp["choices"][0]["message"]["content"]
        except Exception:
            log.warning("topic_llm: unexpected response shape")
            continue

        parsed = _parse_indexed_topics(str(content), expected=len(batch))
        for i, (sess, brief) in enumerate(batch):
            entry = parsed.get(i) or {}
            topic_raw = (entry.get("topic") or "").strip().strip('"').strip("'")
            topic_long_raw = (entry.get("topic_long") or "").strip().strip('"').strip("'")

            # Scrub app names from the short topic so the model can't sneak
            # them past the prompt instruction.
            topic_clean = _scrub_app_names(topic_raw, brief.get("app", ""))
            topic_clean = re.sub(r"\s+", " ", topic_clean).strip(" ,;:-—–")

            # Word-count guard: 2–20 words for short topic.
            word_count = len(topic_clean.split())
            if not topic_clean or word_count < 2 or word_count > 20:
                continue

            sess["topic"] = topic_clean[:250]
            if topic_long_raw:
                # 1–2 sentences target ≈ ≤ 280 chars (small headroom over 250).
                sess["topic_long"] = topic_long_raw[:280]
            set_count += 1

    return set_count


# Common app-name tokens that must never appear standalone in a short topic.
_APP_NAME_TOKENS = {
    "claude", "chatgpt", "chat-gpt", "gemini", "grok", "copilot",
    "webstorm", "pycharm", "intellij", "vscode", "vs code", "code",
    "cursor", "xcode", "sublime", "atom", "neovim", "vim",
    "terminal", "iterm", "iterm2", "warp", "ghostty",
    "safari", "chrome", "firefox", "arc", "edge", "brave", "opera",
    "finder", "preview", "vorschau", "textedit", "notes", "notizen",
    "mail", "outlook", "thunderbird", "messages", "nachrichten",
    "slack", "discord", "whatsapp", "telegram", "signal", "zoom", "teams",
    "spotify", "musik", "music", "podcasts",
    "lm studio", "ollama", "lmstudio",
    "ticktick", "things", "todoist",
    "photopea", "photoshop", "illustrator", "figma", "sketch",
    "calendar", "kalender",
}


def _scrub_app_names(topic: str, primary_app: str) -> str:
    """Remove app-name tokens (whole words) from a topic string.

    Targets *standalone* occurrences only — won't touch e.g. "Claude-Code"
    when "Claude" is a word inside a hyphenated identifier the user wants
    to keep. Connecting words and punctuation around removed tokens are
    cleaned up afterwards.
    """
    if not topic:
        return ""

    tokens = set(_APP_NAME_TOKENS)
    if primary_app:
        tokens.add(primary_app.strip().lower())

    # Build a regex that matches any token as a whole word, case-insensitive.
    # Sort longest first so multi-word tokens like "lm studio" win over "lm".
    sorted_tokens = sorted(tokens, key=len, reverse=True)
    pattern = r"\b(?:" + "|".join(re.escape(t) for t in sorted_tokens if t) + r")\b"
    cleaned = re.sub(pattern, "", topic, flags=re.IGNORECASE)

    # Tidy up: drop dangling connectors like "in", "mit", "bei", "auf", "im"
    # left behind, plus stray punctuation/whitespace.
    cleaned = re.sub(
        r"\b(in|im|mit|bei|auf|von|via|durch|für|in der|in dem)\s+(?=[\s,;:.-]|$)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Collapse multi-whitespace, but keep internal hyphens (city-snake, Topic-LLM).
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Clean up runs of punctuation that got isolated by removals.
    cleaned = re.sub(r"\s*[,;:]+\s*", ", ", cleaned)
    cleaned = re.sub(r",\s*,+", ",", cleaned)
    # Trim leading/trailing separators (incl. dangling hyphens at edges).
    cleaned = cleaned.strip(" ,;:-—–")
    return cleaned
