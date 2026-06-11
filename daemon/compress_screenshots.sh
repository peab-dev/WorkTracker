#!/bin/bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# compress_screenshots.sh — Convert WorkTracker PNG screenshots to JPEG
# Uses macOS native `sips` (no external deps). Default quality: 75.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

set -euo pipefail

# ── Screenshots path resolution (env → config.yaml → default) ────────────────
# Precedence:
#   1. $SCREENSHOTS_DIR if exported by caller (wt compress sets this).
#   2. collector.screenshot.dir from ~/WorkTracker/daemon/config.yaml.
#   3. Hard-coded default ~/WorkTracker/data/screenshots (first-run safety).
_DEFAULT_SCREENSHOTS_DIR="$HOME/WorkTracker/data/screenshots"
_CONFIG_FILE="$HOME/WorkTracker/daemon/config.yaml"
_VENV_PY="$HOME/WorkTracker/daemon/.venv/bin/python"
if [[ -z "${SCREENSHOTS_DIR:-}" ]]; then
    # Try reading from config. Falls back silently if python/yaml/config missing.
    _from_cfg=""
    if [[ -x "$_VENV_PY" && -f "$_CONFIG_FILE" ]]; then
        _from_cfg=$("$_VENV_PY" -c "
import os, sys, yaml
try:
    with open('$_CONFIG_FILE') as f:
        cfg = yaml.safe_load(f) or {}
    p = cfg.get('collector', {}).get('screenshot', {}).get('dir')
    if p: print(os.path.expanduser(p))
except Exception:
    pass
" 2>/dev/null) || _from_cfg=""
    fi
    SCREENSHOTS_DIR="${_from_cfg:-$_DEFAULT_SCREENSHOTS_DIR}"
    unset _from_cfg
fi
unset _DEFAULT_SCREENSHOTS_DIR _CONFIG_FILE _VENV_PY

QUALITY=75
TARGET_DATE=""
SKIP_TODAY=false
DRY_RUN=false
MIN_AGE_SECONDS=10   # race-guard against the running collector

# Cache log (set by `wt compress` to LOG_DIR/wt-cache.log). Days that were
# fully compressed are recorded as "COMPRESS <day> <dir-mtime> ts=<iso>" and
# skipped on later runs as long as the directory mtime is unchanged.
CACHE_FILE="${WT_CACHE_FILE:-}"

# ── Colors (TTY only) ────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    RST=$'\033[0m'; BOLD=$'\033[1m'; DIM=$'\033[2m'
    RED=$'\033[38;5;203m'; GREEN=$'\033[38;5;114m'; YELLOW=$'\033[38;5;221m'
    CYAN=$'\033[38;5;154m'; GRAY=$'\033[38;5;240m'; GRAY_L=$'\033[38;5;245m'
    WHITE=$'\033[38;5;252m'
else
    RST=""; BOLD=""; DIM=""
    RED=""; GREEN=""; YELLOW=""
    CYAN=""; GRAY=""; GRAY_L=""
    WHITE=""
fi

# ── Helpers ──────────────────────────────────────────────────────────────────
human_size() {
    local bytes="${1:-0}"
    awk -v b="$bytes" 'BEGIN {
        if      (b < 1024)       printf "%dB",   b
        else if (b < 1048576)    printf "%dK",   b/1024
        else if (b < 1073741824) printf "%.1fM", b/1048576
        else                     printf "%.1fG", b/1073741824
    }'
}

die() { printf "%s✗%s  %s\n" "$RED" "$RST" "$1" >&2; exit 1; }

# Is this day recorded as fully compressed with an unchanged dir mtime?
cache_is_done() {
    local day="$1" dir="$2"
    [[ -n "$CACHE_FILE" && -f "$CACHE_FILE" ]] || return 1
    local dm
    dm=$(stat -f %m "$dir" 2>/dev/null || echo 0)
    grep -q "^COMPRESS $day $dm " "$CACHE_FILE"
}

# Record a day as fully compressed (rewrites its line, keeps everything else).
cache_mark_done() {
    local day="$1" dir="$2"
    [[ -n "$CACHE_FILE" ]] || return 0
    local dm ts tmp
    dm=$(stat -f %m "$dir" 2>/dev/null || echo 0)
    ts=$(date '+%Y-%m-%dT%H:%M:%S')
    tmp="${CACHE_FILE}.tmp.$$"
    mkdir -p "$(dirname "$CACHE_FILE")"
    {
        grep -v "^COMPRESS $day " "$CACHE_FILE" 2>/dev/null || true
        printf 'COMPRESS %s %s ts=%s\n' "$day" "$dm" "$ts"
    } > "$tmp"
    mv "$tmp" "$CACHE_FILE"
}

usage() {
    cat <<EOF
Usage: compress_screenshots.sh [options]

Converts PNG screenshots under SCREENSHOTS_DIR/<YYYY-MM-DD>/*.png to JPEG.
Originals are deleted only after a successful conversion.

Options:
  --quality N        JPEG quality 1-100 (default: 75)
  --date YYYY-MM-DD  Compress only this one day
  --skip-today       Skip today's folder (default: included)
  --dry-run          Show estimated savings without modifying files
  --help, -h         Show this help

Environment:
  SCREENSHOTS_DIR    Base dir (default: ~/WorkTracker/data/screenshots)
EOF
}

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --quality)
            [[ $# -ge 2 ]] || die "--quality needs a value"
            QUALITY="$2"
            [[ "$QUALITY" =~ ^[0-9]+$ ]] || die "--quality must be an integer (got: $QUALITY)"
            (( QUALITY >= 1 && QUALITY <= 100 )) || die "--quality must be 1-100"
            shift 2
            ;;
        --date)
            [[ $# -ge 2 ]] || die "--date needs a value"
            TARGET_DATE="$2"
            [[ "$TARGET_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "--date must be YYYY-MM-DD"
            shift 2
            ;;
        --skip-today) SKIP_TODAY=true; shift ;;
        --dry-run)    DRY_RUN=true;    shift ;;
        --help|-h)    usage; exit 0 ;;
        *)            die "Unknown option: $1 (use --help)" ;;
    esac
done

# ── Sanity checks ────────────────────────────────────────────────────────────
command -v sips >/dev/null 2>&1 || die "sips not found (macOS required)"
[[ -d "$SCREENSHOTS_DIR" ]] || die "Screenshots dir not found: $SCREENSHOTS_DIR"

# ── Collect target dirs ──────────────────────────────────────────────────────
TODAY=$(date +%Y-%m-%d)
dirs=()

if [[ -n "$TARGET_DATE" ]]; then
    one="$SCREENSHOTS_DIR/$TARGET_DATE"
    [[ -d "$one" ]] || die "No directory for date $TARGET_DATE: $one"
    dirs+=("$one")
else
    shopt -s nullglob
    candidates=("$SCREENSHOTS_DIR"/????-??-??)
    shopt -u nullglob
    for d in "${candidates[@]}"; do
        [[ -d "$d" ]] || continue
        day=$(basename "$d")
        if $SKIP_TODAY && [[ "$day" == "$TODAY" ]]; then
            continue
        fi
        dirs+=("$d")
    done
fi

if [[ ${#dirs[@]} -eq 0 ]]; then
    printf "%s◆%s  No date directories to process.\n" "$YELLOW" "$RST"
    exit 0
fi

# ── Header ───────────────────────────────────────────────────────────────────
mode_label="compress"
$DRY_RUN && mode_label="dry-run"
plural=""
(( ${#dirs[@]} != 1 )) && plural="s"

printf "  %s%sScreenshot %s%s  %s(JPEG q%d, %d dir%s)%s\n" \
    "$CYAN" "$BOLD" "$mode_label" "$RST" "$GRAY_L" "$QUALITY" "${#dirs[@]}" "$plural" "$RST"
printf "  %s%s%s%s\n\n" "$GRAY" "$DIM" "$SCREENSHOTS_DIR" "$RST"

# ── Cache pre-pass: skip past days already fully compressed ─────────────────
process_dirs=()
cached_dirs=0
for dir in "${dirs[@]}"; do
    day=$(basename "$dir")
    if [[ "$day" != "$TODAY" ]] && cache_is_done "$day" "$dir"; then
        cached_dirs=$(( cached_dirs + 1 ))
    else
        process_dirs+=("$dir")
    fi
done

if (( cached_dirs > 0 )); then
    printf "  %sSkipped (cached): %d day(s) — see wt-cache.log%s\n\n" \
        "$GRAY_L" "$cached_dirs" "$RST"
fi

# ── Process each directory ──────────────────────────────────────────────────
now_epoch=$(date +%s)
SPIN=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')

total_ok=0
total_skip=0
total_fail=0
total_before=0
total_after=0

for dir in "${process_dirs[@]}"; do
    day=$(basename "$dir")

    shopt -s nullglob
    pngs=("$dir"/*.png)
    shopt -u nullglob

    if [[ ${#pngs[@]} -eq 0 ]]; then
        printf "  %s%-12s%s  %s%sno PNGs%s\n" \
            "$GRAY_L" "$day" "$RST" "$GRAY" "$DIM" "$RST"
        if [[ "$day" != "$TODAY" ]] && ! $DRY_RUN; then
            cache_mark_done "$day" "$dir"
        fi
        continue
    fi

    dir_ok=0
    dir_skip=0
    dir_fail=0
    dir_before=0
    dir_after=0
    total_pngs=${#pngs[@]}

    for png in "${pngs[@]}"; do
        mtime=$(stat -f %m "$png" 2>/dev/null || echo 0)
        age=$(( now_epoch - mtime ))
        if (( age < MIN_AGE_SECONDS )); then
            dir_skip=$(( dir_skip + 1 ))
            continue
        fi

        bytes_before=$(stat -f %z "$png" 2>/dev/null || echo 0)
        dir_before=$(( dir_before + bytes_before ))

        jpg="${png%.png}.jpg"
        tmp="${png%.png}.tmp.$$.jpg"

        if $DRY_RUN; then
            # q75 JPEG ≈ 40% of PNG size (empirical average over UI screenshots)
            est=$(( bytes_before * 40 / 100 ))
            dir_after=$(( dir_after + est ))
            dir_ok=$(( dir_ok + 1 ))
            continue
        fi

        if [[ -f "$jpg" ]]; then
            # already converted (resumed after interruption)
            rm -f "$png"
            bytes_after=$(stat -f %z "$jpg" 2>/dev/null || echo 0)
            dir_after=$(( dir_after + bytes_after ))
            dir_ok=$(( dir_ok + 1 ))
            continue
        fi

        if sips -s format jpeg -s formatOptions "$QUALITY" "$png" -o "$tmp" >/dev/null 2>&1; then
            mv "$tmp" "$jpg"
            rm -f "$png"
            bytes_after=$(stat -f %z "$jpg" 2>/dev/null || echo 0)
            dir_after=$(( dir_after + bytes_after ))
            dir_ok=$(( dir_ok + 1 ))
        else
            rm -f "$tmp" 2>/dev/null || true
            dir_fail=$(( dir_fail + 1 ))
        fi

        if [[ -t 1 ]]; then
            printf "\r  %s%s%s  %s%-12s  %d/%d%s " \
                "$CYAN" "${SPIN[$(( (dir_ok + dir_fail) % 10 ))]}" "$RST" \
                "$GRAY_L" "$day" $(( dir_ok + dir_fail )) "$total_pngs" "$RST"
        fi
    done

    [[ -t 1 ]] && printf "\r\033[K"

    before_h=$(human_size "$dir_before")
    after_h=$(human_size "$dir_after")
    if (( dir_before > 0 )); then
        pct=$(awk -v b="$dir_before" -v a="$dir_after" \
            'BEGIN { printf "%d", (b - a) * 100 / b }')
    else
        pct=0
    fi

    extra=""
    (( dir_skip > 0 )) && extra="$extra ${YELLOW}${dir_skip} skipped${RST}"
    (( dir_fail > 0 )) && extra="$extra ${RED}${dir_fail} failed${RST}"

    printf "  %s%s%-12s%s  %s%5d files%s  %s%8s%s %s→%s %s%8s%s  %s(-%d%%)%s%s\n" \
        "$WHITE" "$BOLD" "$day" "$RST" \
        "$GRAY_L" "$dir_ok" "$RST" \
        "$WHITE" "$before_h" "$RST" \
        "$GRAY" "$RST" \
        "$GREEN" "$after_h" "$RST" \
        "$CYAN" "$pct" "$RST" \
        "$extra"

    total_ok=$(( total_ok + dir_ok ))
    total_skip=$(( total_skip + dir_skip ))
    total_fail=$(( total_fail + dir_fail ))
    total_before=$(( total_before + dir_before ))
    total_after=$(( total_after + dir_after ))

    if [[ "$day" != "$TODAY" ]] && ! $DRY_RUN \
        && (( dir_fail == 0 && dir_skip == 0 )); then
        cache_mark_done "$day" "$dir"
    fi
done

echo ""

# ── Totals ───────────────────────────────────────────────────────────────────
total_before_h=$(human_size "$total_before")
total_after_h=$(human_size "$total_after")
if (( total_before > 0 )); then
    total_pct=$(awk -v b="$total_before" -v a="$total_after" \
        'BEGIN { printf "%d", (b - a) * 100 / b }')
    saved=$(( total_before - total_after ))
    saved_h=$(human_size "$saved")
else
    total_pct=0
    saved_h="0B"
fi

dry_note=""
$DRY_RUN && dry_note=" ${YELLOW}${BOLD}(~estimate, dry-run)${RST}"

printf "  %s%sTotal%s         %s%s%5d files%s  %s%8s%s %s→%s %s%s%8s%s  %s%s(-%d%%, %s saved)%s%s\n" \
    "$CYAN" "$BOLD" "$RST" \
    "$WHITE" "$BOLD" "$total_ok" "$RST" \
    "$WHITE" "$total_before_h" "$RST" \
    "$GRAY" "$RST" \
    "$GREEN" "$BOLD" "$total_after_h" "$RST" \
    "$CYAN" "$BOLD" "$total_pct" "$saved_h" "$RST" \
    "$dry_note"

(( total_skip > 0 )) && printf "  %sSkipped (young):  %d%s\n" "$GRAY_L" "$total_skip" "$RST"

if (( total_fail > 0 )); then
    printf "  %sFailed:           %d%s\n" "$RED" "$total_fail" "$RST"
    exit 1
fi

exit 0
