#!/bin/bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WorkTracker Uninstaller
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -euo pipefail

RST='\033[0m'
BOLD='\033[1m'
RED='\033[38;5;203m'
GREEN='\033[38;5;114m'
CYAN='\033[38;5;117m'
WHITE='\033[38;5;252m'
GRAY='\033[38;5;240m'

ok()   { printf "  ${GREEN}✓${RST}  %b\n" "$1"; }
info() { printf "  ${CYAN}▸${RST}  %b\n" "$1"; }

WT_HOME="$HOME/WorkTracker"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

echo ""
printf "  ${RED}${BOLD}WorkTracker Uninstaller${RST}\n"
echo ""

read -p "  Really uninstall WorkTracker? (y/N) " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "  Cancelled."
    exit 0
fi

echo ""

# Unload and remove launchd services
info "Unloading launchd services..."
for name in collector aggregator.daily aggregator.weekly aggregator.monthly; do
    local_plist="$LAUNCH_AGENTS/com.peab.worktracker.${name}.plist"
    launchctl unload "$local_plist" 2>/dev/null || true
    rm -f "$local_plist"
done
ok "launchd services removed"

# Remove shell aliases
info "Removing shell aliases..."
for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    if [[ -f "$rc" ]] && grep -q "# >>> WorkTracker >>>" "$rc"; then
        sed -i '' '/# >>> WorkTracker >>>/,/# <<< WorkTracker <<</d' "$rc"
        ok "Aliases removed from $(basename "$rc")"
    fi
done

# Keep data?
echo ""
read -p "  Delete collected data as well? (y/N) " del_data
if [[ "$del_data" == "y" || "$del_data" == "Y" ]]; then
    info "Deleting ${WHITE}$WT_HOME${RST}..."
    rm -rf "$WT_HOME"
    ok "Everything deleted"
else
    info "Removing program code only (data preserved)..."
    rm -rf "$WT_HOME/daemon/.venv"
    rm -rf "$WT_HOME/launchd"
    ok "Program code removed, data preserved in ${WHITE}$WT_HOME/data${RST}"
fi

echo ""
ok "${WHITE}${BOLD}WorkTracker uninstalled.${RST}"
echo ""
