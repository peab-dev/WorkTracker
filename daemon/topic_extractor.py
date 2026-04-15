"""Topic extraction for WorkTracker sessions via a LOCAL LLM.

Sends minimal session context (app name, stripped window title, url host,
filename, project, duration) to a local OpenAI-compatible chat completions
endpoint (LM Studio, Ollama, etc.) and writes a short topic string back
into each session.

Never sends clipboard samples, ambient window titles, or anything that was
not already going into reports. Failure modes (timeout, bad JSON, endpoint
down) are swallowed — the aggregator keeps working with empty topics.
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
    "Du bekommst ein JSON-Array von Arbeits-Sessions, jede mit einem 'idx'-Feld. "
    "Für JEDE Session extrahierst du EIN kurzes Thema (max. 6 Wörter, Deutsch), "
    "das beschreibt WAS inhaltlich gemacht wird (nicht den App-Namen). "
    "Du gibst NUR ein JSON-Array zurück, je Eintrag ein Objekt der Form "
    '{"idx": <zahl>, "topic": "<thema>"} — ein Objekt pro Input-Session, '
    "in beliebiger Reihenfolge. Keine Prosa, keine Code-Fences, keine Kommentare. "
    "Wenn du aus den Infos kein sinnvolles Thema ableiten kannst, lasse "
    "'topic' leer ('')."
)


_APP_NAME_SINGLE_WORD = {
    "claude", "chatgpt", "gemini", "grok", "photopea", "finder",
    "textedit", "preview", "vorschau", "safari", "chrome", "arc",
    "firefox", "terminal", "cursor", "code", "xcode",
}


def _is_too_thin(brief: dict) -> bool:
    """True when there's basically nothing content-ish for the LLM to work with."""
    title = (brief.get("title") or "").strip()
    filename = (brief.get("filename") or "").strip()
    host = (brief.get("host") or "").strip()
    if filename or host:
        return False
    if not title:
        return True
    low = title.lower()
    if low in _APP_NAME_SINGLE_WORD:
        return True
    # Single word that equals the app name (case-insensitive)
    app = (brief.get("app") or "").strip().lower()
    if app and low == app:
        return True
    return False


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


def _session_brief(session: dict, title_suffixes: Iterable[str]) -> dict:
    raw_title = str(session.get("window_title") or "")
    semantic_title = _strip_suffixes(raw_title, title_suffixes)
    return {
        "app": session.get("app_name") or "",
        "title": semantic_title[:160],
        "host": _host(session.get("url") or ""),
        "filename": _filename(raw_title),
        "project": session.get("project") or "",
        "duration_min": round(int(session.get("duration_seconds", 0)) / 60),
    }


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


def _parse_indexed_topics(text: str, expected: int) -> dict[int, str]:
    """Extract a dict {idx: topic} from model output.

    Accepts either an array of objects ``[{"idx":0,"topic":"…"}, …]`` or,
    as fallback, a plain array of strings mapped positionally.
    """
    if not text:
        return {}
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    data = None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if not isinstance(data, list):
        return {}

    out: dict[int, str] = {}
    for i, item in enumerate(data):
        if isinstance(item, dict):
            idx = item.get("idx")
            topic = item.get("topic", "")
            if isinstance(idx, int) and 0 <= idx < expected:
                out[idx] = str(topic or "").strip()
        elif isinstance(item, str) and i < expected:
            # Positional fallback
            out[i] = item.strip()
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
        user_msg = (
            "Input:\n"
            + json.dumps(briefs_with_idx, ensure_ascii=False)
            + f'\n\nOutput (JSON-Array, {len(batch)} Objekte, jeweils '
            '{"idx": <zahl>, "topic": "<kurzes Thema>"}):'
        )
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

        idx_to_topic = _parse_indexed_topics(str(content), expected=len(batch))
        for i, (sess, brief) in enumerate(batch):
            topic = idx_to_topic.get(i, "").strip().strip('"').strip("'")
            if not topic:
                continue
            # Reject topics that are just the app name (hallucination guard)
            app = (brief.get("app") or "").strip().lower()
            if topic.lower() == app or topic.lower() in _APP_NAME_SINGLE_WORD:
                continue
            sess["topic"] = topic[:120]
            set_count += 1

    return set_count
