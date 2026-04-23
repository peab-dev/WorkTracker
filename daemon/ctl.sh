#!/bin/bash
# WorkTracker — Daemon & Aggregator Control Script

LABEL="com.peab.worktracker.collector"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

AGG_DAILY="com.peab.worktracker.aggregator.daily"
AGG_WEEKLY="com.peab.worktracker.aggregator.weekly"
AGG_MONTHLY="com.peab.worktracker.aggregator.monthly"

# ── Paths from config.yaml (fall back to the canonical ~/WorkTracker layout) ──
_BASE="$HOME/WorkTracker"
_VENV_PY="$_BASE/daemon/.venv/bin/python"
_CONFIG="$_BASE/daemon/config.yaml"

_cfg_vals=""
if [[ -x "$_VENV_PY" && -f "$_CONFIG" ]]; then
    _cfg_vals=$("$_VENV_PY" -c "
import os, yaml
try:
    with open('$_CONFIG') as f:
        c = yaml.safe_load(f) or {}
    print(os.path.expanduser(c.get('collector', {}).get('log_dir', '$_BASE/logs')))
    print(os.path.expanduser(c.get('aggregator', {}).get('summaries_dir', '$_BASE/summaries')))
except Exception:
    pass
" 2>/dev/null) || _cfg_vals=""
fi
LOG_DIR=$(  printf '%s\n' "$_cfg_vals" | awk 'NR==1{print;exit}')
SUMMARIES=$(printf '%s\n' "$_cfg_vals" | awk 'NR==2{print;exit}')
LOG_DIR="${LOG_DIR:-$_BASE/logs}"
SUMMARIES="${SUMMARIES:-$_BASE/summaries}"
unset _cfg_vals _VENV_PY _CONFIG

case "$1" in
    start)
        echo "Starting $LABEL..."
        launchctl load "$PLIST" 2>/dev/null
        launchctl start "$LABEL"
        sleep 1
        if launchctl list "$LABEL" &>/dev/null; then
            echo "Running. PID: $(launchctl list "$LABEL" | awk 'NR==2{print $1}')"
        else
            echo "Failed to start. Check: $LOG_DIR/collector-stderr.log"
            exit 1
        fi
        ;;
    stop)
        echo "Stopping $LABEL..."
        launchctl stop "$LABEL"
        launchctl unload "$PLIST" 2>/dev/null
        echo "Stopped."
        ;;
    restart)
        echo "Restarting $LABEL..."
        launchctl stop "$LABEL" 2>/dev/null
        launchctl unload "$PLIST" 2>/dev/null
        sleep 1
        launchctl load "$PLIST"
        launchctl start "$LABEL"
        sleep 1
        if launchctl list "$LABEL" &>/dev/null; then
            echo "Running. PID: $(launchctl list "$LABEL" | awk 'NR==2{print $1}')"
        else
            echo "Failed to restart. Check: $LOG_DIR/collector-stderr.log"
            exit 1
        fi
        ;;
    status)
        if launchctl list "$LABEL" &>/dev/null; then
            echo "=== $LABEL ==="
            launchctl list "$LABEL"
            echo ""
            echo "=== Logs ==="
            echo "stdout: $LOG_DIR/collector-stdout.log"
            echo "stderr: $LOG_DIR/collector-stderr.log"
            echo "app:    $LOG_DIR/collector.log"
        else
            echo "$LABEL is not loaded."
        fi
        ;;
    tail)
        echo "Tailing logs (Ctrl+C to stop)..."
        tail -f "$LOG_DIR/collector-stdout.log" "$LOG_DIR/collector-stderr.log" "$LOG_DIR/collector.log"
        ;;
    agg-daily)
        echo "Triggering daily aggregation..."
        launchctl start "$AGG_DAILY"
        sleep 3
        echo "Exit-Status: $(launchctl list "$AGG_DAILY" 2>/dev/null | grep LastExitStatus | awk '{print $NF}' | tr -d '";')"
        echo "Log: $LOG_DIR/aggregator-daily-stderr.log"
        tail -3 "$LOG_DIR/aggregator-daily-stderr.log" 2>/dev/null
        ;;
    agg-weekly)
        echo "Triggering weekly aggregation..."
        launchctl start "$AGG_WEEKLY"
        sleep 3
        echo "Exit-Status: $(launchctl list "$AGG_WEEKLY" 2>/dev/null | grep LastExitStatus | awk '{print $NF}' | tr -d '";')"
        echo "Log: $LOG_DIR/aggregator-weekly-stderr.log"
        tail -3 "$LOG_DIR/aggregator-weekly-stderr.log" 2>/dev/null
        ;;
    agg-monthly)
        echo "Triggering monthly aggregation..."
        launchctl start "$AGG_MONTHLY"
        sleep 3
        echo "Exit-Status: $(launchctl list "$AGG_MONTHLY" 2>/dev/null | grep LastExitStatus | awk '{print $NF}' | tr -d '";')"
        echo "Log: $LOG_DIR/aggregator-monthly-stderr.log"
        tail -3 "$LOG_DIR/aggregator-monthly-stderr.log" 2>/dev/null
        ;;
    agg-status)
        echo "=== Aggregator Jobs ==="
        for job in "$AGG_DAILY" "$AGG_WEEKLY" "$AGG_MONTHLY"; do
            if launchctl list "$job" &>/dev/null; then
                exit_status=$(launchctl list "$job" 2>/dev/null | grep LastExitStatus | awk '{print $NF}' | tr -d '";')
                echo "  $job  loaded  last_exit=$exit_status"
            else
                echo "  $job  NOT loaded"
            fi
        done
        echo ""
        echo "=== Latest Reports ==="
        echo "  daily:   $(ls -t "$SUMMARIES"/daily/*.md 2>/dev/null | head -1 || echo 'none')"
        echo "  weekly:  $(ls -t "$SUMMARIES"/weekly/*.md 2>/dev/null | head -1 || echo 'none')"
        echo "  monthly: $(ls -t "$SUMMARIES"/monthly/*.md 2>/dev/null | head -1 || echo 'none')"
        ;;
    dashboard)
        exec "$HOME/WorkTracker/daemon/.venv/bin/python" "$HOME/WorkTracker/daemon/dashboard.py"
        ;;
    web)
        echo "Starting web dashboard on http://127.0.0.1:7880 ..."
        exec "$HOME/WorkTracker/daemon/.venv/bin/python" "$HOME/WorkTracker/daemon/web_dashboard.py"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|tail|dashboard|web}"
        echo "       $0 {agg-daily|agg-weekly|agg-monthly|agg-status}"
        exit 1
        ;;
esac
