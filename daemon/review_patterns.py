#!/usr/bin/env python3
"""Interactive review of auto-generated pattern suggestions."""

import sys
import termios
import tty
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
LEARNED_PATH = BASE_DIR / "learned_patterns.yaml"
PROJECT_PATH = BASE_DIR / "project_patterns.yaml"

# ── Colors ──────────────────────────────────────────────────────────────────
RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[38;5;154m"
GREEN = "\033[38;5;114m"
YELLOW = "\033[38;5;221m"
WHITE = "\033[38;5;252m"
GRAY = "\033[38;5;240m"
GRAY_L = "\033[38;5;245m"
RED = "\033[38;5;203m"
ORANGE = "\033[38;5;215m"

# ── States ──────────────────────────────────────────────────────────────────
SKIP = 0     # skip (do nothing)
ADOPT = 1    # adopt into project_patterns.yaml
REMOVE = 2   # remove from learned_patterns.yaml

STATE_LABEL = {
    SKIP: f"{GRAY}skip{RST}",
    ADOPT: f"{GREEN}{BOLD}adopt{RST}",
    REMOVE: f"{RED}{BOLD}remove{RST}",
}
STATE_MARKER = {
    SKIP: f"{GRAY}○{RST}",
    ADOPT: f"{GREEN}✓{RST}",
    REMOVE: f"{RED}✗{RST}",
}


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def read_key() -> str:
    """Read a single keypress, handling arrow keys."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 == "A":
                    return "up"
                if ch3 == "B":
                    return "down"
            return "escape"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def save_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def render(suggestions: list[dict], states: list[int], cursor: int) -> None:
    """Render the suggestion list with cursor."""
    sys.stdout.write("\033[2J\033[H")  # clear screen

    print(f"\n  {CYAN}{BOLD}LEARNED PATTERNS — Review{RST}")
    print(f"  {GRAY}{'─' * 55}{RST}\n")

    for i, s in enumerate(suggestions):
        marker = STATE_MARKER[states[i]]
        label = STATE_LABEL[states[i]]
        pointer = f"{CYAN}▸{RST}" if i == cursor else " "
        name = f"{WHITE}{BOLD}{s['name']:20s}{RST}"
        dur = f"{ORANGE}{fmt_duration(s['time']):>6s}{RST}"

        print(f"  {pointer} {marker} {name} {dur}  {label}")

        # Patterns + URL patterns
        patterns = s.get("patterns", [])
        url_patterns = s.get("url_patterns", [])
        all_patterns = patterns + [f"url:{p}" for p in url_patterns]
        if all_patterns:
            print(f"        {DIM}{', '.join(all_patterns)}{RST}")

        # Sample titles
        for title in s.get("samples", [])[:2]:
            print(f"        {GRAY}{title[:65]}{RST}")
        print()

    print(f"  {GRAY}{'─' * 55}{RST}")
    print(f"  {WHITE}{BOLD}↑↓{RST} {GRAY_L}navigate{RST}    "
          f"{WHITE}{BOLD}space{RST} {GRAY_L}cycle state{RST}    "
          f"{WHITE}{BOLD}enter{RST} {GRAY_L}confirm{RST}    "
          f"{WHITE}{BOLD}s{RST} {GRAY_L}skip all{RST}")
    print()


def _yaml_inline_entry(name: str, entry: dict) -> str:
    """Format a project entry in the original compact inline-list YAML style."""
    lines = [f"  {name}:"]
    for key in ("patterns", "url_patterns"):
        vals = entry.get(key)
        if vals:
            items = ", ".join(f'"{v}"' for v in vals)
            lines.append(f"    {key}: [{items}]")
    if entry.get("category"):
        lines.append(f'    category: "{entry["category"]}"')
    return "\n".join(lines)


def apply_decisions(suggestions: list[dict], states: list[int]) -> None:
    """Apply adopt/remove/skip decisions."""
    learned = load_yaml(LEARNED_PATH)
    learned_projects = learned.get("projects", {})
    dismissed = set(learned.get("dismissed", []) or [])

    # Read project_patterns.yaml as raw text to preserve formatting
    text = PROJECT_PATH.read_text()

    adopted = []
    removed = []

    for i, s in enumerate(suggestions):
        name = s["name"]
        state = states[i]

        if state == ADOPT:
            entry = {"patterns": s["patterns"], "category": s["category"]}
            if s.get("url_patterns"):
                entry["url_patterns"] = s["url_patterns"]

            block = _yaml_inline_entry(name, entry)
            if "default_project:" in text:
                text = text.replace("default_project:", f"{block}\n\ndefault_project:", 1)
            else:
                text += f"\n{block}\n"

            adopted.append(name)
            learned_projects.pop(name, None)
            dismissed.add(name)

        elif state == REMOVE:
            removed.append(name)
            learned_projects.pop(name, None)
            dismissed.add(name)

    # Save
    if adopted:
        PROJECT_PATH.write_text(text)
    learned["projects"] = learned_projects
    learned["dismissed"] = sorted(dismissed)
    save_yaml(LEARNED_PATH, learned)

    # Print summary
    for name in adopted:
        print(f"  {GREEN}✓{RST}  {WHITE}{BOLD}{name}{RST} → project_patterns.yaml")
    for name in removed:
        print(f"  {RED}✗{RST}  {GRAY_L}{name}{RST} removed")
    skipped = sum(1 for st in states if st == SKIP)
    if skipped:
        print(f"  {GRAY}○{RST}  {GRAY_L}{skipped} skipped{RST}")


def main() -> None:
    if not sys.stdin.isatty():
        return

    learned = load_yaml(LEARNED_PATH)
    projects = learned.get("projects", {})
    dismissed = set(learned.get("dismissed", []) or [])

    # Filter for auto-generated entries only, excluding previously dismissed ones
    auto = {
        k: v for k, v in projects.items()
        if v.get("_auto_generated") and k not in dismissed
    }

    if not auto:
        return

    # Build suggestion list
    suggestions = []
    for name, info in auto.items():
        suggestions.append({
            "name": name,
            "patterns": info.get("patterns", []),
            "url_patterns": info.get("url_patterns", []),
            "category": info.get("category", "Uncategorized"),
            "time": info.get("_total_time_seconds", 0),
            "samples": info.get("_sample_titles", []),
        })

    # Sort by total time descending
    suggestions.sort(key=lambda x: x["time"], reverse=True)

    states = [SKIP] * len(suggestions)
    cursor = 0

    while True:
        render(suggestions, states, cursor)
        key = read_key()

        if key == "s" or key == "q" or key == "escape":
            sys.stdout.write("\033[2J\033[H")
            print(f"  {GRAY_L}Skipped all.{RST}")
            break

        elif key == "\r" or key == "\n":
            sys.stdout.write("\033[2J\033[H")
            if any(st != SKIP for st in states):
                apply_decisions(suggestions, states)
            else:
                print(f"  {GRAY_L}No changes.{RST}")
            break

        elif key == " ":
            # Cycle: skip → adopt → remove → skip
            states[cursor] = (states[cursor] + 1) % 3

        elif key == "up":
            cursor = (cursor - 1) % len(suggestions)

        elif key == "down":
            cursor = (cursor + 1) % len(suggestions)


if __name__ == "__main__":
    main()
