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
    "Du bist ein Hilfsprogramm, das aus kurzen Arbeits-Session-Infos ein "
    "prägnantes Thema auf Deutsch extrahiert (max. 6 Wörter). "
    "Du antwortest AUSSCHLIESSLICH mit einem JSON-Array von Strings — "
    "ein Eintrag pro Session, in derselben Reihenfolge wie die Eingabe. "
    "Keine Erklärungen, keine Einleitung, nur das Array."
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


def _parse_topics(text: str, expected: int) -> list[str]:
    """Extract a JSON array of strings from model output.

    Local models sometimes wrap JSON in prose or Markdown fences.
    This is best-effort: return an array of length *expected* or [].
    """
    if not text:
        return []
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    # First attempt: direct JSON
    try:
        data = json.loads(text)
    except Exception:
        # Find first [...] block
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    topics = [str(x).strip() for x in data]
    # Trim to expected length
    if len(topics) < expected:
        topics += [""] * (expected - len(topics))
    return topics[:expected]


def extract_topics(sessions: list[dict], cfg: dict,
                    title_suffixes: "list[str] | tuple[str, ...]" = ()) -> int:
    """Annotate sessions in place with a ``topic`` field. Returns count set.

    Silent on all failures — if anything goes wrong, sessions keep
    ``topic == ""``.
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

    candidates = [
        s for s in sessions
        if int(s.get("duration_seconds", 0)) >= min_dur
        and not str(s.get("topic") or "").strip()
    ][:max_sessions]

    if not candidates:
        return 0

    set_count = 0
    for chunk_start in range(0, len(candidates), batch_size):
        batch = candidates[chunk_start: chunk_start + batch_size]
        briefs = [_session_brief(s, title_suffixes) for s in batch]
        user_msg = (
            "Hier sind "
            + str(len(briefs))
            + " Sessions als JSON-Liste. Gib für jede ein kurzes Thema "
            "(max. 6 Wörter, Deutsch) zurück — als JSON-Array in gleicher Reihenfolge.\n\n"
            + json.dumps(briefs, ensure_ascii=False)
        )
        payload = {
            "model": model,
            "temperature": 0.2,
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
        topics = _parse_topics(str(content), expected=len(batch))
        for s, t in zip(batch, topics):
            t = (t or "").strip().strip('"').strip("'")
            if t:
                s["topic"] = t[:120]
                set_count += 1

    return set_count
