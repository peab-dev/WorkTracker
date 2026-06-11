#!/usr/bin/env python3
"""history_stats.py — incremental Full-History stats for `wt status`.

Caches per-day counts/sizes in the wt cache log (LOG_DIR/wt-cache.log) and
recomputes only days whose underlying files changed since the last run.
Today is always recomputed fresh and never cached (still being written).

Cache record format (one line per day, human-readable):
  STATUS <day> <key> <snap_n> <snap_b> <sess_n> <sess_b> <scr_n> <scr_b> ts=<iso>

<key> encodes mtime+size of the snapshot/session files and mtime of the
screenshot dir — if any of them changed, the day is recomputed.
Non-STATUS lines (e.g. COMPRESS records from compress_screenshots.sh) are
preserved untouched.

Usage:
  history_stats.py SNAP_DIR SESS_DIR SCR_DIR CACHE_FILE

Output (stdout, single line):
  snap_count|snap_bytes|sess_count|sess_bytes|scr_count|scr_bytes|refreshed
"""
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

IMG_EXTS = {'.png', '.jpg', '.jpeg'}
DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')  # excludes AppleDouble ._* files


def day_key(snap: Path, sess: Path, scr: Path) -> str:
    def file_key(p: Path) -> str:
        try:
            st = p.stat()
            return f"{int(st.st_mtime)}-{st.st_size}"
        except OSError:
            return "x"

    def dir_key(p: Path) -> str:
        try:
            return str(int(p.stat().st_mtime))
        except OSError:
            return "x"

    return f"{file_key(snap)}:{file_key(sess)}:{dir_key(scr)}"


def compute_day(snap: Path, sess: Path, scr: Path) -> list:
    snap_n = snap_b = sess_n = sess_b = scr_n = scr_b = 0
    try:
        if snap.is_file():
            snap_b = snap.stat().st_size
            with open(snap, 'rb') as f:
                snap_n = sum(1 for _ in f)
    except OSError:
        pass
    try:
        if sess.is_file():
            sess_b = sess.stat().st_size
            with open(sess) as f:
                sess_n = len(json.load(f))
    except (OSError, ValueError):
        pass
    try:
        if scr.is_dir():
            for e in os.scandir(scr):
                if not e.is_file() or e.name.startswith('._'):
                    continue
                if os.path.splitext(e.name)[1].lower() in IMG_EXTS:
                    scr_n += 1
                    scr_b += e.stat().st_size
    except OSError:
        pass
    return [snap_n, snap_b, sess_n, sess_b, scr_n, scr_b]


def main() -> None:
    snap_dir, sess_dir, scr_dir, cache_file = (Path(a) for a in sys.argv[1:5])
    today = date.today().isoformat()

    days = set()
    for base, suffix in ((snap_dir, '.jsonl'), (sess_dir, '.json')):
        if base.is_dir():
            days.update(f.name[:-len(suffix)] for f in base.glob(f'*{suffix}')
                        if DAY_RE.match(f.name[:-len(suffix)]))
    if scr_dir.is_dir():
        days.update(d.name for d in scr_dir.iterdir()
                    if d.is_dir() and DAY_RE.match(d.name))

    cached = {}
    other_lines = []
    if cache_file.is_file():
        for line in cache_file.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 9 and parts[0] == 'STATUS':
                cached[parts[1]] = (parts, line)
            elif line.strip():
                other_lines.append(line)

    totals = [0] * 6
    refreshed = 0
    status_lines = []
    for d in sorted(days):
        snap = snap_dir / f'{d}.jsonl'
        sess = sess_dir / f'{d}.json'
        scr = scr_dir / d
        if d == today:
            vals = compute_day(snap, sess, scr)
        else:
            key = day_key(snap, sess, scr)
            hit = cached.get(d)
            if hit and hit[0][2] == key:
                vals = [int(x) for x in hit[0][3:9]]
                status_lines.append(hit[1])  # keep original ts
            else:
                vals = compute_day(snap, sess, scr)
                refreshed += 1
                ts = time.strftime('%Y-%m-%dT%H:%M:%S')
                status_lines.append(
                    'STATUS %s %s %s ts=%s'
                    % (d, key, ' '.join(str(v) for v in vals), ts))
        totals = [a + b for a, b in zip(totals, vals)]

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_name(cache_file.name + '.tmp')
    tmp.write_text('\n'.join(other_lines + status_lines) + '\n')
    tmp.replace(cache_file)

    print('|'.join(str(v) for v in totals + [refreshed]))


if __name__ == '__main__':
    main()
