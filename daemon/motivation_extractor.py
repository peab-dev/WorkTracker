"""Extract motivation messages for WorkTracker sessions with a local vision LLM.

Sends up to N screenshots per session to a local, OpenAI-compatible
chat/completions endpoint (LM Studio with a vision model, Ollama-LLaVA, etc.)
and writes a short English motivation message to ``session["motivation_message"]``.

Screenshots may contain ANY screen content, including passwords, email, and
private data, so the configured endpoint MUST be local. Never point it at a
remote endpoint.

Failure modes such as timeouts, invalid JSON, an unavailable endpoint, or an
oversized image are silent; the aggregator continues with an empty
``motivation_message``.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from urllib import request as _urlrequest
from urllib.error import URLError, HTTPError

# Child of the "aggregator" logger so warnings reach aggregator.log —
# under "worktracker.*" they had no handler and were silently dropped.
log = logging.getLogger("aggregator.motivation_extractor")

_SYSTEM_PROMPT = (
    "/no_think\n"
    "You receive ONE screenshot from a work session plus minimal metadata "
    "(app, project, duration). Analyze the image carefully: which app, file, "
    "code, text, tab, UI element, numbers, or charts are visible? Mention at "
    "least one specific visible detail such as a filename, function, variable, "
    "class, window title, terminal command, icon, error, editor line, URL, or "
    "button. Do not rely only on the metadata.\n\n"
    "Write 2 to 4 English sentences (maximum 100 words) that acknowledge the "
    "work and encourage the user to continue. Keep the tone warm, concrete, "
    "and specific. Avoid generic phrases such as 'stay focused' or 'keep it "
    "up'. If no concrete detail is visible, say so honestly.\n\n"
    "No greeting, emojis, code fences, or leading label. Return prose only."
)


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


def _encode_image(path: str, max_bytes: int) -> "str | None":
    try:
        p = Path(path)
        if not p.is_file():
            return None
        raw = p.read_bytes()
        if len(raw) > max_bytes:
            return None
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return None


def _sample_paths(paths: list[str], max_count: int) -> list[str]:
    """Pick up to *max_count* evenly-spaced paths from the list."""
    if max_count <= 0 or not paths:
        return []
    if len(paths) <= max_count:
        return list(paths)
    step = len(paths) / max_count
    return [paths[int(i * step)] for i in range(max_count)]


def _clean_motivation(text: str) -> str:
    s = (text or "").strip()
    # Strip chain-of-thought blocks from reasoning models (Qwen3, etc.) that
    # some servers inline into `content` instead of `reasoning_content`.
    s = re.sub(
        r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>",
        "",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    s = re.sub(
        r"<think(?:ing)?\b[^>]*>.*",
        "",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        # Remove a possible "json\n" prefix or language tag.
        if "\n" in s:
            s = s.split("\n", 1)[1]
    s = s.strip().strip('"').strip("'").strip()
    if len(s) > 1000:
        s = s[:1000].rstrip()
    return s


def extract_motivations(sessions: list[dict], cfg: dict,
                         progress: "callable | None" = None) -> int:
    """Annotate sessions in place with ``motivation_message``. Returns count set.

    Silent on all failures.
    """
    mcfg = (cfg or {}).get("aggregator", {}).get("motivation_llm", {}) or {}
    if not mcfg.get("enabled"):
        return 0

    endpoint = mcfg.get("endpoint") or ""
    if not endpoint:
        return 0
    model = mcfg.get("model", "local-vision-model")
    timeout = float(mcfg.get("timeout_seconds", 30))
    max_images = int(mcfg.get("max_images_per_session", 6))
    max_sessions = int(mcfg.get("max_sessions_per_day", 40))
    min_dur = int(mcfg.get("min_session_seconds", 300))
    image_max_bytes = int(mcfg.get("image_max_bytes", 1_500_000))

    candidates: list[dict] = []
    for s in sessions:
        if str(s.get("motivation_message") or "").strip():
            continue
        if int(s.get("duration_seconds", 0) or 0) < min_dur:
            continue
        paths = s.get("screenshot_paths") or []
        if not isinstance(paths, list) or not paths:
            continue
        candidates.append(s)
        if len(candidates) >= max_sessions:
            break

    if not candidates:
        if progress:
            try:
                progress(0, 0, 0)
            except Exception:
                pass
        return 0

    total = len(candidates)
    set_count = 0
    for i, sess in enumerate(candidates, start=1):
        if progress:
            try:
                progress(i, total, set_count)
            except Exception:
                pass

        sampled = _sample_paths(list(sess.get("screenshot_paths") or []), max_images)
        encoded = [u for u in (_encode_image(p, image_max_bytes) for p in sampled) if u]
        if not encoded:
            continue

        dur_min = round(int(sess.get("duration_seconds", 0) or 0) / 60)
        meta = (
            f"App: {sess.get('app_name','')} \u00b7 "
            f"Project: {sess.get('project','')} \u00b7 "
            f"Duration: {dur_min} min"
        )
        topic = str(sess.get("topic") or "").strip()
        if topic:
            meta += f" \u00b7 Topic: {topic}"

        content_parts: list[dict] = [{"type": "text", "text": meta}]
        for data_uri in encoded:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": data_uri},
            })

        payload = {
            "model": model,
            "temperature": 0.5,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content_parts},
            ],
        }
        try:
            resp = _post_json(endpoint, payload, timeout)
        except HTTPError as e:
            try:
                body_snippet = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                body_snippet = ""
            log.warning("motivation_llm HTTP %s: %s | body=%s",
                        e.code, e.reason, body_snippet)
            continue
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            log.warning("motivation_llm request failed: %s", e)
            continue
        except Exception as e:  # pragma: no cover
            log.warning("motivation_llm unexpected error: %s", e)
            continue

        try:
            content = resp["choices"][0]["message"]["content"]
        except Exception:
            log.warning("motivation_llm: unexpected response shape")
            continue

        text = _clean_motivation(str(content or ""))
        if not text:
            continue
        words = text.split()
        if len(words) < 3 or len(words) > 120:
            continue
        sess["motivation_message"] = text
        set_count += 1

    if progress:
        try:
            progress(total, total, set_count)
        except Exception:
            pass

    return set_count
