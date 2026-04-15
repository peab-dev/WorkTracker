"""Motivations-Extraktion fuer WorkTracker-Sessions via VISION-LLM (lokal).

Sendet pro Session bis zu N Screenshots an einen lokalen, OpenAI-kompatiblen
chat/completions-Endpoint (LM Studio mit Vision-Modell, Ollama-LLaVA, etc.)
und schreibt einen kurzen deutschen Motivationssatz in ``session["motivation_message"]``.

Weil Screenshots BELIEBIGE Bildschirminhalte enthalten koennen (Passwoerter,
Mails, Privates), MUSS der konfigurierte Endpoint lokal sein. Auf keinen Fall
auf einen Remote-Endpoint zeigen.

Failure-Modi (Timeout, schlechtes JSON, Endpoint down, Bild zu gross) sind
still — der Aggregator laeuft mit leerem ``motivation_message`` weiter.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from urllib import request as _urlrequest
from urllib.error import URLError

log = logging.getLogger("worktracker.motivation_extractor")

_SYSTEM_PROMPT = (
    "Du bekommst EIN Screenshot einer Arbeits-Session und minimale Metadaten "
    "(App, Projekt, Dauer). Analysiere das Bild GENAU: Welche App ist offen, "
    "welche Datei, welcher Code, welcher Text, welcher Tab, welches "
    "UI-Element, welche Zahlen oder Diagramme sind zu sehen? Gehe im Text "
    "konkret auf mindestens ein sichtbares Detail ein (Dateiname, "
    "Funktionsname, Variablenname, Klassenname, Fenstertitel, Terminal-Befehl, "
    "Icon, Fehlermeldung, Zeile im Editor, URL, Button, etc.) — nicht nur auf "
    "die Metadaten.\n\n"
    "Schreibe 2 bis 4 deutsche Saetze (max. 100 Woerter), die den Nutzer "
    "anerkennend zum Weitermachen einladen. Der Ton ist warm, konkret, "
    "spezifisch — keine generischen Floskeln wie 'konzentriere dich weiter' "
    "oder 'mach weiter so'. Wenn nichts Konkretes im Bild erkennbar ist, "
    "benenne genau DAS ehrlich.\n\n"
    "Keine Anrede, keine Emojis, keine Code-Fences, kein Label davor — nur "
    "den Fliesstext."
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
    if s.startswith("```"):
        s = s.strip("`")
        # entferne moegliches "json\n" oder Sprach-Tag
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
            f"Projekt: {sess.get('project','')} \u00b7 "
            f"Dauer: {dur_min} min"
        )
        topic = str(sess.get("topic") or "").strip()
        if topic:
            meta += f" \u00b7 Thema: {topic}"

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
