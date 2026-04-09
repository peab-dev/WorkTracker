#!/bin/bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WorkTracker Installer
# Installs WorkTracker on a new Mac
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────────
RST='\033[0m'
BOLD='\033[1m'
BLINK='\033[5m'
RED='\033[38;5;203m'
GREEN='\033[38;5;114m'
CYAN='\033[38;5;117m'
GRAY='\033[38;5;240m'
WHITE='\033[38;5;252m'

ok()   { printf "  ${GREEN}✓${RST}  %b\n" "$1"; }
err()  { printf "  ${RED}✗${RST}  %b\n" "$1"; }
info() { printf "  ${CYAN}▸${RST}  %b\n" "$1"; }
line() { printf "${GRAY}"; printf '%*s' "$(tput cols 2>/dev/null || echo 60)" '' | tr ' ' '─'; printf "${RST}\n"; }

header() {
    echo ""
    printf "  ${CYAN}${BOLD}██╗    ██╗       ████████╗${RST}\n"
    printf "  ${CYAN}${BOLD}██║    ██║       ╚══██╔══╝${RST}\n"
    printf "  ${CYAN}${BOLD}██║ █╗ ██║ ${WHITE}${BLINK}██${RST}${CYAN}${BOLD}    ██║${RST}\n"
    printf "  ${CYAN}${BOLD}██║███╗██║ ${WHITE}${BLINK}██${RST}${CYAN}${BOLD}    ██║${RST}\n"
    printf "  ${CYAN}${BOLD}╚███╔███╔╝       ██║${RST}\n"
    printf "  ${CYAN}${BOLD} ╚══╝╚══╝        ╚═╝${RST}\n"
    echo ""
    printf "  ${GRAY}WorkTracker Installer${RST}\n"
    line
    echo ""
}

# ── Configuration ───────────────────────────────────────────────────────────
WT_HOME="$HOME/WorkTracker"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$WT_HOME/daemon/.venv"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

# ── Helper: Yes/No prompt ───────────────────────────────────────────────────
ask_yn() {
    local prompt="$1"
    local answer
    read -p "  $prompt (y/N) " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]]
}

# ── Prerequisites ───────────────────────────────────────────────────────────
check_prereqs() {
    info "Checking prerequisites..."

    # macOS?
    if [[ "$(uname)" != "Darwin" ]]; then
        err "WorkTracker only runs on macOS"
        exit 1
    fi
    ok "macOS detected"

    # Xcode Command Line Tools (needed for pip/compilation)
    if ! xcode-select -p &>/dev/null; then
        info "Xcode Command Line Tools not found"
        if ask_yn "Install Xcode Command Line Tools?"; then
            info "Installing Xcode Command Line Tools..."
            xcode-select --install
            echo ""
            info "Please confirm the installation in the dialog."
            info "Then run ${WHITE}./install.sh${RST} again."
            exit 0
        else
            err "Xcode Command Line Tools are required"
            exit 1
        fi
    fi
    ok "Xcode Command Line Tools found"

    # Homebrew
    if ! command -v brew &>/dev/null; then
        info "Homebrew not found"
        if ask_yn "Install Homebrew? (recommended)"; then
            info "Installing Homebrew..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

            # Load Homebrew into current shell
            if [[ -f "/opt/homebrew/bin/brew" ]]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [[ -f "/usr/local/bin/brew" ]]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
            ok "Homebrew installed"
        else
            info "Continuing without Homebrew (Python must be installed manually)"
        fi
    else
        ok "Homebrew found"
    fi

    # Python 3
    if ! command -v python3 &>/dev/null; then
        info "Python 3 not found"
        if command -v brew &>/dev/null; then
            if ask_yn "Install Python 3 via Homebrew?"; then
                info "Installing Python 3..."
                brew install python3
                ok "Python 3 installed"
            else
                err "Python 3 is required"
                exit 1
            fi
        else
            err "Python 3 not found and Homebrew not available"
            info "Install Python manually: ${WHITE}https://www.python.org/downloads/${RST}"
            exit 1
        fi
    fi

    local pyver
    pyver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    ok "Python ${pyver} found"

    echo ""
}

# ── Copy project (if needed) ────────────────────────────────────────────────
setup_project() {
    if [[ "$SCRIPT_DIR" == "$WT_HOME" ]]; then
        info "Project already in ${WHITE}$WT_HOME${RST}"
    else
        info "Copying project to ${WHITE}$WT_HOME${RST}..."
        if [[ -d "$WT_HOME" ]]; then
            err "$WT_HOME already exists!"
            printf "     Please remove first or run installer from ${WHITE}$WT_HOME${RST}.\n"
            exit 1
        fi
        cp -R "$SCRIPT_DIR" "$WT_HOME"
        ok "Project copied"
    fi
}

# ── Directories ─────────────────────────────────────────────────────────────
setup_dirs() {
    info "Creating directories..."
    mkdir -p "$WT_HOME/data/snapshots"
    mkdir -p "$WT_HOME/data/sessions"
    mkdir -p "$WT_HOME/logs"
    mkdir -p "$WT_HOME/summaries/daily"
    mkdir -p "$WT_HOME/summaries/weekly"
    mkdir -p "$WT_HOME/summaries/monthly"
    ok "Directories created"
}

# ── Python venv ─────────────────────────────────────────────────────────────
setup_venv() {
    info "Creating Python virtual environment..."

    if [[ -d "$VENV" ]]; then
        info "venv already exists, skipping"
    else
        python3 -m venv "$VENV"
        ok "venv created"
    fi

    info "Installing dependencies..."
    "$VENV/bin/pip" install --upgrade pip --quiet
    "$VENV/bin/pip" install -r "$WT_HOME/daemon/requirements.txt" --quiet
    "$VENV/bin/pip" install flask rapidfuzz --quiet
    ok "Dependencies installed"
}

# ── Generate launchd plist files ────────────────────────────────────────────
generate_plists() {
    info "Generating launchd configuration..."

    local PYTHON="$VENV/bin/python"
    local DAEMON_DIR="$WT_HOME/daemon"
    local LOG_DIR="$WT_HOME/logs"

    mkdir -p "$WT_HOME/launchd"

    # Collector (KeepAlive, RunAtLoad)
    cat > "$WT_HOME/launchd/com.peab.worktracker.collector.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.peab.worktracker.collector</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${DAEMON_DIR}/collector.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/collector-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/collector-stderr.log</string>
    <key>WorkingDirectory</key>
    <string>${DAEMON_DIR}</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>10</integer>
</dict>
</plist>
PLIST

    # Daily Aggregator (22:00)
    cat > "$WT_HOME/launchd/com.peab.worktracker.aggregator.daily.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.peab.worktracker.aggregator.daily</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${DAEMON_DIR}/aggregator.py</string>
        <string>--mode</string>
        <string>daily</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>22</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/aggregator-daily-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/aggregator-daily-stderr.log</string>
    <key>WorkingDirectory</key>
    <string>${DAEMON_DIR}</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>10</integer>
</dict>
</plist>
PLIST

    # Weekly Aggregator (Sunday 23:00)
    cat > "$WT_HOME/launchd/com.peab.worktracker.aggregator.weekly.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.peab.worktracker.aggregator.weekly</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${DAEMON_DIR}/aggregator.py</string>
        <string>--mode</string>
        <string>weekly</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>23</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/aggregator-weekly-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/aggregator-weekly-stderr.log</string>
    <key>WorkingDirectory</key>
    <string>${DAEMON_DIR}</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>10</integer>
</dict>
</plist>
PLIST

    # Monthly Aggregator (1st of month 00:30)
    cat > "$WT_HOME/launchd/com.peab.worktracker.aggregator.monthly.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.peab.worktracker.aggregator.monthly</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${DAEMON_DIR}/aggregator.py</string>
        <string>--mode</string>
        <string>monthly</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Day</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>0</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/aggregator-monthly-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/aggregator-monthly-stderr.log</string>
    <key>WorkingDirectory</key>
    <string>${DAEMON_DIR}</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>10</integer>
</dict>
</plist>
PLIST

    ok "launchd configuration generated"
}

# ── Install launchd ────────────────────────────────────────────────────────
install_launchd() {
    info "Installing launchd services..."
    mkdir -p "$LAUNCH_AGENTS"

    local plists=(
        "com.peab.worktracker.collector"
        "com.peab.worktracker.aggregator.daily"
        "com.peab.worktracker.aggregator.weekly"
        "com.peab.worktracker.aggregator.monthly"
    )

    for name in "${plists[@]}"; do
        local src="$WT_HOME/launchd/${name}.plist"
        local dst="$LAUNCH_AGENTS/${name}.plist"

        # Unload first if already loaded
        launchctl unload "$dst" 2>/dev/null || true

        cp "$src" "$dst"
        launchctl load "$dst" 2>/dev/null || true
    done

    ok "launchd services installed and loaded"
}

# ── Install wt CLI ──────────────────────────────────────────────────────────
install_cli() {
    info "Installing ${WHITE}wt${RST} CLI..."

    chmod +x "$WT_HOME/wt"

    # Find shell configuration
    local shell_rc=""
    if [[ -f "$HOME/.zshrc" ]]; then
        shell_rc="$HOME/.zshrc"
    elif [[ -f "$HOME/.bashrc" ]]; then
        shell_rc="$HOME/.bashrc"
    elif [[ -f "$HOME/.bash_profile" ]]; then
        shell_rc="$HOME/.bash_profile"
    fi

    local marker_start="# >>> WorkTracker >>>"
    local marker_end="# <<< WorkTracker <<<"
    local block
    read -r -d '' block << 'ALIASES' || true
# >>> WorkTracker >>>
export PATH="$HOME/WorkTracker:$PATH"
alias wts='wt status'
alias wtl='wt tail'
alias wtr='wt restart'
alias wtd='wt daily'
alias wtw='wt weekly'
alias wtm='wt monthly'
alias wtx='wt status && echo "---" && wt daily && echo "---" && wt weekly && echo "---" && wt monthly'
alias wtdash='wt dash'
alias wtrh='wt rhythm'
alias wtweb='wt web'
alias wtmb='wt menubar'
alias wtdocs='wt docs'
alias wtdocu='wt docs'
alias wtdocumentation='wt docs'
alias wtrl='exec $SHELL -l'
# <<< WorkTracker <<<
ALIASES

    if [[ -n "$shell_rc" ]]; then
        if grep -q "$marker_start" "$shell_rc" 2>/dev/null; then
            # Replace existing block to ensure all aliases are up to date
            local tmp
            tmp=$(mktemp)
            awk -v start="$marker_start" -v end="$marker_end" -v newblock="$block" '
                $0 == start { skip=1; printed=0; next }
                $0 == end   { skip=0; if (!printed) { print newblock; printed=1 }; next }
                skip { next }
                { print }
            ' "$shell_rc" > "$tmp"
            # Ensure new block is present (in case awk didn't print it)
            if ! grep -q "$marker_start" "$tmp"; then
                echo "" >> "$tmp"
                echo "$block" >> "$tmp"
            fi
            mv "$tmp" "$shell_rc"
            ok "Aliases updated in ${WHITE}$(basename "$shell_rc")${RST}"
        else
            echo "" >> "$shell_rc"
            echo "$block" >> "$shell_rc"
            ok "Aliases added to ${WHITE}$(basename "$shell_rc")${RST}"
        fi
    else
        # Fallback: create .zshrc (macOS default shell)
        shell_rc="$HOME/.zshrc"
        echo "$block" > "$shell_rc"
        ok "Aliases written to ${WHITE}.zshrc${RST}"
    fi
}

# ── macOS Permissions ───────────────────────────────────────────────────────
check_permissions() {
    echo ""
    info "${WHITE}${BOLD}Important: macOS Permissions${RST}"
    echo ""
    printf "  WorkTracker needs access to:\n"
    printf "  ${CYAN}1.${RST} ${WHITE}Accessibility${RST} — for keyboard/mouse events\n"
    printf "  ${CYAN}2.${RST} ${WHITE}Screen Recording${RST} — for window titles\n"
    echo ""
    printf "  Go to: ${WHITE}System Settings → Privacy & Security${RST}\n"
    printf "  and grant ${WHITE}Terminal${RST} (or your terminal app) access.\n"
    echo ""
}

# ── Main ────────────────────────────────────────────────────────────────────
main() {
    header
    check_prereqs
    setup_project
    setup_dirs
    setup_venv
    generate_plists
    install_launchd
    install_cli

    echo ""
    line
    echo ""
    ok "${WHITE}${BOLD}WorkTracker successfully installed!${RST}"
    echo ""
    printf "  Start a new shell or run:\n"
    printf "    ${CYAN}source ~/.zshrc${RST}\n"
    echo ""
    printf "  Then use:\n"
    printf "    ${CYAN}wt status${RST}   — Show status\n"
    printf "    ${CYAN}wt help${RST}     — Show all commands\n"
    echo ""

    check_permissions

    line
    echo ""
}

main "$@"
