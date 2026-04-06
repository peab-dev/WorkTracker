# WorkTracker — Umsetzungsplan v3 (Final)

## Projektübersicht

Automatisches Tracking der Mac-Arbeitsaktivität alle 10 Sekunden mit dateibasierter JSONL-Speicherung, Pandas-basiertem Aggregator und Claude Cowork als AI-Analyse-Layer. Rein privates, lokales Tool — keine Einschränkungen bei der Datenerfassung.

---

## Architektur

```
┌─────────────────────────────────────────────────────────┐
│  LAYER 1 — DATENSAMMLUNG (Python Daemon, alle 10s)      │
│  collector.py → JSONL-Dateien (1 File pro Tag)          │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│  LAYER 2 — AGGREGATION (Python + Pandas, per launchd)   │
│  aggregator.py → Sessions-JSON + Markdown-Export        │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│  LAYER 3 — AI-ANALYSE (Claude Cowork, geplante Tasks)   │
│  Liest Markdown → Generiert Summaries + Vorschläge      │
└─────────────────────────────────────────────────────────┘
```

---

## Dateistruktur

```
~/WorkTracker/
├── daemon/
│   ├── collector.py
│   ├── aggregator.py
│   ├── config.yaml
│   ├── project_patterns.yaml
│   └── requirements.txt
├── launchd/
│   ├── com.peab.worktracker.collector.plist
│   └── com.peab.worktracker.aggregator.plist
├── data/
│   ├── snapshots/                # JSONL, 1 Datei pro Tag
│   └── sessions/                 # Aggregierte Sessions
├── summaries/
│   ├── daily/
│   ├── weekly/
│   └── monthly/
├── logs/
└── README.md
```

---

## Umsetzung in 7 Steps

---

# ════════════════════════════════════════════
# STEP 1 — Projekt-Setup
# Tool: Claude Code
# Dauer: 5 Minuten
# ════════════════════════════════════════════

## Prompt für Claude Code:

```
Erstelle die komplette Ordnerstruktur und Basis-Dateien für mein
WorkTracker-Projekt auf macOS (M4 Mac Mini, macOS Sequoia).

Erstelle folgende Struktur unter ~/WorkTracker/:

~/WorkTracker/
├── daemon/
│   ├── config.yaml
│   ├── project_patterns.yaml
│   └── requirements.txt
├── data/
│   ├── snapshots/
│   └── sessions/
├── summaries/
│   ├── daily/
│   ├── weekly/
│   └── monthly/
├── launchd/
├── logs/
└── README.md

config.yaml Inhalt:
  collector:
    interval_seconds: 10
    data_dir: ~/WorkTracker/data/snapshots
    log_dir: ~/WorkTracker/logs
    track_clipboard_content: true    # Vollständiger Inhalt erlaubt
    track_input_counts: true
    track_media: true
    track_all_windows: true
  aggregator:
    sessions_dir: ~/WorkTracker/data/sessions
    summaries_dir: ~/WorkTracker/summaries
    idle_threshold_seconds: 120
    focus_session_min_seconds: 1500
    fuzzy_match_threshold: 0.7

project_patterns.yaml Inhalt:
  projects:
    easyAMS:
      patterns: ["*easyams*", "*easyAMS*", "*easy-ams*"]
      category: "Development"
    Austro Intelligence:
      patterns: ["*austro*intelligence*", "*ai.peab*", "*austro*intel*"]
      category: "Business"
    Hippieparty:
      patterns: ["*hippieparty*", "*hippie*party*"]
      category: "Creative"
    Psychedelic Animals:
      patterns: ["*psychedelic*animal*"]
      category: "Creative"
    PEAB:
      patterns: ["*peab.at*", "*peab*"]
      category: "Development"
    Dr. Überflieger:
      patterns: ["*überflieger*", "*ueberflieger*", "*suno*"]
      category: "Music"
    AI Research:
      patterns: ["*claude*", "*openai*", "*chatgpt*", "*anthropic*"]
      category: "AI/Research"
    Communication:
      patterns: ["*gmail*", "*slack*", "*whatsapp*", "*telegram*", "*mail*"]
      category: "Communication"
    Social Media:
      patterns: ["*instagram*", "*twitter*", "*x.com*", "*reddit*", "*tiktok*"]
      category: "Social Media"
    Crypto:
      patterns: ["*binance*", "*coinbase*", "*coingecko*", "*tradingview*"]
      category: "Crypto"
    Media:
      patterns: ["*youtube*", "*spotify*", "*netflix*", "*twitch*"]
      category: "Media/Entertainment"
  default_project: "Sonstiges"

requirements.txt:
  pyobjc-framework-Cocoa>=10.0
  pyobjc-framework-Quartz>=10.0
  pandas>=2.2
  pyyaml>=6.0
  python-Levenshtein>=0.25

Dann:
1. Erstelle ein Python venv unter ~/WorkTracker/daemon/.venv
2. Installiere die Dependencies aus requirements.txt
3. Erstelle ein README.md mit Projektbeschreibung und Setup-Anleitung
4. Gib mir eine Zusammenfassung was erstellt wurde und ob alles
   funktioniert hat.
```

---

# ════════════════════════════════════════════
# STEP 2 — Collector Daemon
# Tool: Claude Code
# Dauer: 30-60 Minuten
# ════════════════════════════════════════════

## Prompt für Claude Code:

```
Erstelle den WorkTracker Collector-Daemon: ein Python-Script
"collector.py" für macOS (M4 Mac Mini, macOS Sequoia) das als
Background-Daemon läuft und alle 10 Sekunden System-Aktivitätsdaten
sammelt und in JSONL-Dateien schreibt.

Speicherort: ~/WorkTracker/daemon/collector.py
Config: ~/WorkTracker/daemon/config.yaml (existiert bereits)
Venv: ~/WorkTracker/daemon/.venv (existiert bereits)
Ausgabe: ~/WorkTracker/data/snapshots/YYYY-MM-DD.jsonl
Format: Eine JSON-Zeile pro Snapshot, append-only

Das ist ein rein privates lokales Tool nur für mich. Es gibt
KEINE Datenschutz-Einschränkungen. Alle Daten dürfen vollständig
erfasst und gespeichert werden.

DATEN DIE GESAMMELT WERDEN:

1. AKTIVES FENSTER (pyobjc / AppKit / Quartz):
   - NSWorkspace.shared().frontmostApplication → App-Name, Bundle-ID
   - CGWindowListCopyWindowInfo → Fenstertitel des aktiven Fensters
   - Der Fenstertitel ist extrem wertvoll: enthält bei Browsern die
     URL/Seitentitel, bei VS Code das Projekt und Datei, bei Finder
     den Pfad, etc.

2. ALLE SICHTBAREN FENSTER:
   - CGWindowListCopyWindowInfo mit kCGWindowListOptionOnScreenOnly
   - Für jedes Fenster: App-Name, Fenstertitel, Position, Größe,
     ob es das aktive ist
   - Das ermöglicht Erkennung von parallelen Aktivitäten
     (z.B. YouTube im Hintergrund)

3. ALLE LAUFENDEN APPS:
   - NSWorkspace.shared().runningApplications
   - Für jede: Name, Bundle-ID, isActive, isHidden
   - Nur User-facing Apps filtern (activationPolicy == .regular)

4. MEDIA PLAYBACK:
   Versuche über MRMediaRemoteGetNowPlayingInfo:
   - Aktueller Track/Video-Titel
   - Artist/Channel
   - App die abspielt
   - Playback-Status (playing/paused/stopped)
   Falls MRMediaRemote nicht funktioniert, parse stattdessen
   die Fenstertitel von bekannten Media-Apps (Spotify, Music, etc.)

5. INPUT-INTENSITÄT:
   - Keystroke-Count über CGEventTap seit letztem Intervall
     (nur Anzahl, Inhalt wird hier nicht getrackt)
   - Maus: Position, Bewegungsdistanz seit letztem Snapshot,
     Click-Count (links/rechts), Scroll-Events
   - Idle-Time: CGEventSourceSecondsSinceLastEventType
     für Keyboard und Mouse getrennt

6. CLIPBOARD:
   - NSPasteboard.generalPasteboard()
   - changeCount überwachen — bei Änderung:
     - Content-Type (text/image/file/etc.)
     - Bei Text: vollständiger Textinhalt speichern
     - Bei Files: Dateinamen und Pfade
     - Länge/Größe
     - Quell-App wenn ermittelbar

7. SYSTEM:
   - Aktiver Desktop/Space
   - Batterie-Level + Charging-Status
   - Bildschirmhelligkeit (wenn abrufbar)

JSONL SCHEMA PRO ZEILE:

{
  "ts": "2026-04-06T14:23:10.000Z",
  "active_app": {
    "name": "Visual Studio Code",
    "bundle_id": "com.microsoft.VSCode",
    "window_title": "index.html — easyAMS"
  },
  "visible_windows": [
    {
      "app": "Safari",
      "bundle_id": "com.apple.Safari",
      "title": "YouTube — Psytrance Mix 2026",
      "is_active": false,
      "position": {"x": 0, "y": 0},
      "size": {"w": 1920, "h": 1080}
    }
  ],
  "running_apps": [
    {"name": "Safari", "bundle_id": "com.apple.Safari", "active": false, "hidden": false},
    {"name": "VS Code", "bundle_id": "com.microsoft.VSCode", "active": true, "hidden": false}
  ],
  "media": {
    "title": "Psytrance Mix 2026",
    "artist": "DJ XY",
    "app": "Safari",
    "state": "playing"
  },
  "input": {
    "keystrokes": 47,
    "mouse_distance_px": 2340,
    "mouse_clicks_left": 10,
    "mouse_clicks_right": 2,
    "scroll_events": 8,
    "mouse_position": {"x": 960, "y": 540},
    "idle_seconds_keyboard": 0.3,
    "idle_seconds_mouse": 0.1
  },
  "clipboard": {
    "changed": true,
    "type": "text",
    "content": "const handleClick = () => {",
    "length": 28,
    "source_app": "Safari"
  },
  "system": {
    "active_space": 1,
    "battery_pct": 87,
    "battery_charging": false
  }
}

ANFORDERUNGEN:

- PERFORMANCE: <1% CPU, <50MB RAM. Ultra-lean.
- APPEND-ONLY: Tagesdatei im Append-Modus öffnen, eine Zeile
  schreiben, flushen. File-Handle NICHT offen halten.
- TAGESWECHSEL: Um Mitternacht automatisch neue Datei.
- ROBUSTHEIT: Wenn eine Datenquelle fehlschlägt (z.B. Media nicht
  verfügbar), trotzdem alle anderen sammeln. Fehlende Felder als
  null schreiben, niemals crashen.
- ACCESSIBILITY: Beim Start prüfen ob Accessibility und Input
  Monitoring Permissions gesetzt sind. Wenn nicht: klare Anleitung
  ausgeben wie man sie aktiviert, dann graceful beenden.
- LOGGING: ~/WorkTracker/logs/collector.log mit Rotation (max 5 MB,
  3 Backup-Files). Log-Level aus config.yaml lesen.
- CONFIG: Alle Einstellungen aus ~/WorkTracker/daemon/config.yaml
- SIGNAL HANDLING: Sauberes Shutdown bei SIGTERM/SIGINT.
- STARTUP: Beim Start eine Log-Zeile mit allen erkannten
  Datenquellen und deren Status ausgeben.

ERSTELLE:
1. ~/WorkTracker/daemon/collector.py — Der Hauptdaemon
2. Teste das Script: Starte es, lass es 30 Sekunden laufen,
   prüfe ob die JSONL-Datei korrekt geschrieben wird.
3. Zeig mir ein paar Zeilen der generierten JSONL-Datei.
4. Berichte ob alle Datenquellen funktionieren oder ob
   Permissions fehlen.
```

---

# ════════════════════════════════════════════
# STEP 3 — Collector als Daemon einrichten
# Tool: Claude Code
# Dauer: 10 Minuten
# ════════════════════════════════════════════

## Prompt für Claude Code:

```
Der WorkTracker Collector (~/WorkTracker/daemon/collector.py)
funktioniert. Jetzt muss er als permanenter Background-Daemon
eingerichtet werden über macOS launchd.

Erstelle ~/WorkTracker/launchd/com.peab.worktracker.collector.plist
mit folgenden Eigenschaften:

- Label: com.peab.worktracker.collector
- ProgramArguments: Python aus dem venv aufrufen mit collector.py
  (~/WorkTracker/daemon/.venv/bin/python
   ~/WorkTracker/daemon/collector.py)
- RunAtLoad: true (startet beim Login automatisch)
- KeepAlive: true (startet neu wenn er crasht)
- StandardOutPath: ~/WorkTracker/logs/collector-stdout.log
- StandardErrorPath: ~/WorkTracker/logs/collector-stderr.log
- WorkingDirectory: ~/WorkTracker/daemon
- ProcessType: Background
- Nice: 10 (niedrige Priorität)

Dann:
1. Kopiere/symlinke die plist nach ~/Library/LaunchAgents/
2. Lade den Agent: launchctl load ...
3. Prüfe ob er läuft: launchctl list | grep worktracker
4. Prüfe ob Daten geschrieben werden nach ein paar Sekunden
5. Zeig mir wie ich den Daemon stoppe/starte/neustarte
6. Erstelle ein kleines Helper-Script ~/WorkTracker/daemon/ctl.sh:
   - ./ctl.sh start — Daemon starten
   - ./ctl.sh stop — Daemon stoppen
   - ./ctl.sh restart — Daemon neustarten
   - ./ctl.sh status — Status anzeigen
   - ./ctl.sh tail — Live-Log anzeigen
```

---

# ════════════════════════════════════════════
# STEP 4 — Aggregator mit Pandas
# Tool: Claude Code
# Dauer: 30-60 Minuten
# (Voraussetzung: mindestens 1 Tag Collector-Daten)
# ════════════════════════════════════════════

## Prompt für Claude Code:

```
Erstelle den WorkTracker Aggregator: ein Python-Script das
JSONL-Rohdaten aus dem Collector mit Pandas aufbereitet und als
strukturierte Markdown-Dateien exportiert.

Speicherort: ~/WorkTracker/daemon/aggregator.py
Config: ~/WorkTracker/daemon/config.yaml
Projekt-Patterns: ~/WorkTracker/daemon/project_patterns.yaml
Venv: ~/WorkTracker/daemon/.venv (existiert, pandas ist installiert)

Input: ~/WorkTracker/data/snapshots/YYYY-MM-DD.jsonl
Output:
  - ~/WorkTracker/data/sessions/YYYY-MM-DD.json
  - ~/WorkTracker/summaries/daily/YYYY-MM-DD.md
  - ~/WorkTracker/summaries/weekly/YYYY-WXX.md
  - ~/WorkTracker/summaries/monthly/YYYY-MM.md

Modus über CLI:
  python aggregator.py --mode daily [--date 2026-04-06]
  python aggregator.py --mode weekly [--date 2026-04-06]
  python aggregator.py --mode monthly [--date 2026-04-06]

Ohne --date wird das aktuelle Datum / die aktuelle Woche / der
aktuelle Monat verwendet.

═══════════════════════════════════════
KERN-LOGIK
═══════════════════════════════════════

1. DATEN LADEN:
   df = pd.read_json("path.jsonl", lines=True)
   Verschachtelte Felder mit pd.json_normalize() flach machen.
   Timestamp als DatetimeIndex setzen.

2. SESSION-ERKENNUNG:
   Zusammenhängende Snapshots gruppieren wo:
   - active_app.name ist gleich UND
   - active_app.window_title ist ähnlich
     (Levenshtein-Ratio > 0.7, konfigurierbar)
   - Kein Idle-Gap > 120 Sekunden dazwischen

   Jede Session bekommt:
   - start, end, duration_seconds
   - app_name, app_bundle_id
   - window_title (häufigster Titel in der Session)
   - project (aus project_patterns.yaml)
   - category
   - keystrokes_total, mouse_clicks_total
   - intensity_score (normalisiert: keystrokes+clicks / duration)
   - clipboard_events (Liste der Clipboard-Änderungen in der Session)
   - parallel_media (was lief im Hintergrund)

   Sessions als JSON speichern nach data/sessions/YYYY-MM-DD.json

3. PROJEKT-ZUORDNUNG:
   Lies project_patterns.yaml.
   Matche Fenstertitel per fnmatch-Wildcards.
   Erstes Match gewinnt (Reihenfolge in YAML = Priorität).
   Kein Match → default_project ("Sonstiges")

4. PANDAS-BERECHNUNGEN:

   Zeitverteilung:
   - sessions.groupby("project")["duration_seconds"].sum()
   - sessions.groupby("app_name")["duration_seconds"].sum()
   - Stündliche Verteilung: groupby(start.dt.hour)

   Produktivität:
   - Focus-Sessions: sessions[duration_seconds > 1500]
   - App-Wechsel: Anzahl wo app_name != app_name.shift(1) in Snapshots
   - App-Wechsel pro Stunde
   - Keystroke-Intensität pro Stunde (sum keystrokes groupby hour)

   Patterns:
   - Clipboard-Transfers: Bei clipboard.changed == True,
     zähle source_app → active_app Paare
   - Parallel-Media: Zeitanteil wo media.state == "playing"
     und media.app != active_app.name
   - Idle-Ratio: Anteil Snapshots mit idle_seconds > 30

   Vergleich:
   - Tages-Modus: Lade Vortages-Sessions für Delta-Berechnung
   - Wochen-Modus: Lade Vorwochen-Aggregate
   - Monats-Modus: Lade Vormonats-Aggregate

═══════════════════════════════════════
MARKDOWN-OUTPUT: TAGES-REPORT
═══════════════════════════════════════

# WorkTracker — [Wochentag], [DD.MM.YYYY]

## Überblick
- Aktive Arbeitszeit: Xh XXmin
- Zeitraum: HH:MM – HH:MM
- Focus-Sessions (>25min): X (gesamt Xh XXmin)
- App-Wechsel: XXX (Ø XX/h)
- Idle-Anteil: XX%
- Paralleles Media: XX% der Arbeitszeit

## Projektverteilung
| Projekt | Zeit | Anteil | Sessions | Ø Session | Intensität |
|---------|------|--------|----------|-----------|------------|
| easyAMS | 2h 15min | 34% | 5 | 27min | ████████░░ |
| Austro Intelligence | 1h 40min | 25% | 3 | 33min | ██████░░░░ |
| ... | ... | ... | ... | ... | ... |

## App-Nutzung
| App | Zeit | Anteil | Top-Projekt |
|-----|------|--------|-------------|
| VS Code | 3h 10min | 48% | easyAMS |
| Safari | 1h 20min | 20% | AI Research |
| ... | ... | ... | ... |

## Timeline
| Von | Bis | Dauer | App | Kontext | Projekt | Intensität |
|-----|-----|-------|-----|---------|---------|------------|
| 09:12 | 09:47 | 35min | VS Code | easyAMS/index.html | easyAMS | ████████░░ |
| 09:47 | 09:55 | 8min | Safari | Gmail Inbox | Communication | ███░░░░░░░ |
| ... | ... | ... | ... | ... | ... | ... |

## Parallele Aktivitäten
- 09:12–10:45: YouTube "Psytrance Mix" lief während VS Code
- 14:00–14:30: Spotify "Deep Focus" während VS Code

## Input-Analyse
- Keystroke-Peak: XX:00–XX:00 (XXX Anschläge)
- Produktivste Stunde: XX:00
- Gesamt-Keystrokes: XXXX
- Gesamt-Clicks: XXX
- Stündliche Intensität:
  08: ░░░░░░░░░░
  09: ████████░░
  10: ██████████
  ...

## Clipboard-Transfers
| Von | Nach | Anzahl | Inhaltstyp |
|-----|------|--------|------------|
| Safari | VS Code | 12x | Text (Code) |
| Finder | Slack | 3x | Files |

## Erkannte Patterns
[Automatisch berechnete Auffälligkeiten:]
- "Nach Slack-Checks folgten Ø Xmin Browser-Nutzung"
- "Längste Focus-Session: XXmin (App — Projekt)"
- "Höchste App-Wechsel-Rate: XX:00–XX:00 (XX Wechsel)"
- "X identische Clipboard-Inhalte kopiert (gleicher Hash)"

## Vergleich Vortag
| Metrik | Heute | Gestern | Delta |
|--------|-------|---------|-------|
| Arbeitszeit | 6h 30min | 7h 10min | ↓ -9% |
| Focus-Sessions | 5 | 3 | ↑ +67% |
| App-Wechsel/h | 12 | 18 | ↑ besser |
| Idle-Anteil | 8% | 15% | ↑ besser |
| Top-Projekt | easyAMS | Austro Intel | — |

═══════════════════════════════════════
MARKDOWN-OUTPUT: WOCHEN-REPORT
═══════════════════════════════════════

Wird nur im --mode weekly generiert.
Lade ALLE JSONL-Dateien der Kalenderwoche in einen DataFrame.

# WorkTracker — Woche [WXX], [DD.MM.] – [DD.MM.YYYY]

## Überblick
[Gleiche Metriken wie Tages-Report, aber für die ganze Woche]

## Tagesvergleich
| Tag | Arbeitszeit | Focus | Wechsel/h | Top-Projekt | Intensität |
|-----|-------------|-------|-----------|-------------|------------|
| Mo  | 7h 20min    | 4     | 14        | easyAMS     | ████████░░ |
| Di  | 6h 10min    | 6     | 9         | Austro Intel| ██████████ |
| ... | ...         | ...   | ...       | ...         | ... |

## Projektverteilung Woche
[Gleiche Tabelle wie Tages-Report, aggregiert]

## Trends
- Produktivster Tag: [Tag] (warum?)
- Schwächster Tag: [Tag] (warum?)
- Focus-Time Trend: Mo→So Verlauf
- Projekt-Shifts über die Woche

## Patterns der Woche
- Wiederkehrende Muster ("Montags immer Admin")
- App-Sequenzen die sich wiederholen
- Media-Konsum-Trend

## Vergleich Vorwoche
[Delta-Tabelle]

═══════════════════════════════════════
MARKDOWN-OUTPUT: MONATS-REPORT
═══════════════════════════════════════

Wird nur im --mode monthly generiert.
Lade ALLE JSONL-Dateien des Monats.

# WorkTracker — [Monatsname] [YYYY]

## Überblick
[Aggregierte Monats-Metriken]

## Wochenvergleich
| Woche | Arbeitszeit | Focus | Top-Projekt | Intensität |
|-------|-------------|-------|-------------|------------|

## Projektverteilung Monat
[Mit Prozent und Stunden]

## Langzeit-Trends
- Arbeitszeit-Entwicklung
- Focus-Time Entwicklung
- Projekt-Verschiebungen

## Top Patterns
[Die stärksten/wiederkehrendsten Muster des Monats]

═══════════════════════════════════════

ERSTELLE:
1. ~/WorkTracker/daemon/aggregator.py
2. Teste mit den vorhandenen Collector-Daten:
   python aggregator.py --mode daily
3. Zeig mir den generierten Markdown-Output.
4. Wenn Fehler auftreten, fixe sie.

Wichtig:
- Nutze das existierende venv und die installierte pandas-Version
- Lies project_patterns.yaml für Projekt-Zuordnung
- Alle Zeitangaben in lokaler Zeit (Europe/Vienna)
- Robustes Error-Handling: Wenn eine Berechnung fehlschlägt,
  überspringe sie mit Platzhalter-Text, crashe nicht
- Logging nach ~/WorkTracker/logs/aggregator.log
```

---

# ════════════════════════════════════════════
# STEP 5 — Aggregator als Scheduled Job
# Tool: Claude Code
# Dauer: 10 Minuten
# ════════════════════════════════════════════

## Prompt für Claude Code:

```
Der WorkTracker Aggregator (~/WorkTracker/daemon/aggregator.py)
funktioniert. Richte jetzt launchd-Jobs ein die ihn automatisch
zu festen Zeiten ausführen.

Erstelle launchd plist-Dateien unter ~/WorkTracker/launchd/:

1. com.peab.worktracker.aggregator.daily.plist
   - Täglich um 22:00
   - Führt aus: .venv/bin/python aggregator.py --mode daily
   - Stdout/Stderr nach ~/WorkTracker/logs/

2. com.peab.worktracker.aggregator.weekly.plist
   - Jeden Sonntag um 22:30
   - Führt aus: .venv/bin/python aggregator.py --mode weekly

3. com.peab.worktracker.aggregator.monthly.plist
   - Am 1. jedes Monats um 00:30 (für den Vormonat)
   - Führt aus: .venv/bin/python aggregator.py --mode monthly

Dann:
1. Registriere alle drei bei launchd
2. Teste den Daily-Job manuell:
   launchctl start com.peab.worktracker.aggregator.daily
3. Prüfe ob der Markdown-Output generiert wurde
4. Erweitere ~/WorkTracker/daemon/ctl.sh um Aggregator-Befehle:
   - ./ctl.sh agg-daily — Daily Aggregation manuell triggern
   - ./ctl.sh agg-weekly — Weekly manuell triggern
   - ./ctl.sh agg-monthly — Monthly manuell triggern
   - ./ctl.sh agg-status — Status aller Aggregator-Jobs
```

---

# ════════════════════════════════════════════
# STEP 6 — Claude Cowork Tasks einrichten
# Tool: Claude Cowork (manuell)
# Dauer: 15 Minuten
# ════════════════════════════════════════════

## Anleitung:
Öffne Claude Desktop → Cowork Modus → erstelle drei geplante Tasks.
Kopiere jeweils den Prompt unten.

### Task 1: Daily Digest

**Name:** WorkTracker Daily Digest
**Zeitplan:** Täglich, 22:15 (15 Min nach dem Aggregator)

**Prompt:**

```
Du bist mein persönlicher Produktivitäts-Analyst.

AUFGABE:
1. Lies die neueste .md Datei aus ~/WorkTracker/summaries/daily/
   die NOCH KEINE zugehörige *-summary.md hat.
   (Beispiel: Wenn 2026-04-06.md existiert aber
   2026-04-06-summary.md nicht → das ist deine Eingabe)

2. Lies auch die letzten 3 *-summary.md Dateien aus dem gleichen
   Ordner für Kontext und Trendvergleich.

3. Erstelle deine Analyse und speichere als:
   ~/WorkTracker/summaries/daily/[DATUM]-summary.md

INHALT DEINER ANALYSE:

## Tagesnarrative
Ein kurzer, menschlich geschriebener Absatz der den Tag beschreibt.
Was war der rote Faden? Was war besonders? Wie war die Energie?
Beispiel: "Ein fokussierter Frontend-Tag. Hauptsächlich easyAMS mit
zwei intensiven Deep-Work-Sessions am Vormittag. Nachmittags mehr
Admin, abends kurzer Crypto-Check."

## Key Metrics & Bewertung
Die 5 wichtigsten Zahlen des Tages. Vergleiche mit dem Durchschnitt
der letzten 3 Tage. Gib eine ehrliche Bewertung: War es ein guter
Arbeitstag oder nicht? Warum?

## Was gut lief
Konkrete Beobachtungen. Nicht "du warst produktiv" sondern
"Die 52-Minuten Focus-Session an easyAMS/parallax.js war dein
längster ununterbrochener Coding-Block diese Woche."

## Was optimiert werden kann
Erkannte Ineffizienzen mit konkreten Zahlen:
- "Du hast 14x zwischen Chrome und VS Code gewechselt zwischen
  14:00 und 15:00 — das riecht nach Copy-Paste-Workflow der
  verbessert werden kann"
- "Nach jedem der 6 Slack-Checks folgten Ø 11min Browser-Zeit
  bevor du zum Code zurückkamst"
- "3 Sessions unter 5 Minuten in Folge um 16:30 — Ablenkungsphase?"

## 3 konkrete Vorschläge für morgen
Umsetzbare Tipps basierend auf den heutigen Daten:
1. Arbeitsweise-Optimierung (z.B. "Starte morgen mit dem
   easyAMS-Projekt, da hattest du heute dort die höchste Intensität
   am Vormittag")
2. Tool/Workflow-Verbesserung (z.B. "Nutze VS Code Live Server
   statt ständig zwischen Editor und Browser zu wechseln")
3. Zeitmanagement (z.B. "Blocke 09:00-11:00 für Deep Work,
   kein Slack/Mail vor 11:00")

## Automatisierungs-Potenzial
Wenn du repetitive Muster erkennst:
- Konkrete Automatisierungen vorschlagen
- Shell-Scripts, Keyboard Shortcuts, Automator Workflows
- Geschätzte Zeitersparnis pro Tag/Woche

Schreibstil: Direkt, konkret, datengetrieben. Du bist ein
smarter analytischer Kollege. Keine Floskeln, keine
Motivationssprüche. Duze mich.
```

---

### Task 2: Weekly Review

**Name:** WorkTracker Weekly Review
**Zeitplan:** Sonntag, 23:00

**Prompt:**

```
Du bist mein Produktivitäts-Analyst für die Wochenanalyse.

AUFGABE:
1. Lies die neueste .md Datei (nicht *-summary.md) aus
   ~/WorkTracker/summaries/weekly/
2. Lies alle *-summary.md aus ~/WorkTracker/summaries/daily/
   die zu dieser Woche gehören (die 7 Daily Digests)
3. Lies die letzten 2 *-summary.md aus
   ~/WorkTracker/summaries/weekly/ zum Vergleich
4. Speichere deine Analyse als:
   ~/WorkTracker/summaries/weekly/[WOCHE]-summary.md

INHALT:

## Wochennarrative
2-3 Absätze die die Woche als Geschichte erzählen.
Hauptprojekte, Fokus-Shifts, Energie-Verlauf über die Woche.

## Woche in Zahlen
| Tag | Arbeitszeit | Focus | Wechsel/h | Top-Projekt | Score |
Markiere den besten und schlechtesten Tag.

## Projektfortschritt
Für jedes aktive Projekt:
- Investierte Zeit + Trend vs. Vorwoche
- Was hat sich verändert? (Fenstertitel-Analyse: neue Dateien,
  neue URLs, neue Tools)
- Momentum: steigend / stagnierend / fallend
- Ist das Zeitinvestment angemessen für die Priorität?

## Muster der Woche
- Produktivste Tageszeit (mit Daten belegt)
- App-Wechsel-Hotspots
- Korrelation: Media-Konsum ↔ Produktivität
- Clipboard-Flows: Content-Routen zwischen Apps
- Wiederkehrende Ablenkungs-Patterns
- Vergleich Wochentage vs. Wochenende

## Vergleich zur Vorwoche
Konkrete Deltas mit Zahlen und Bewertung.

## Top 5 Empfehlungen für nächste Woche
1. Workflow-Optimierung (konkret, basierend auf Daten)
2. Idealer Zeitblock-Plan (wann welches Projekt, basierend
   auf wann du diese Woche am produktivsten warst)
3. Automatisierungs-Idee (basierend auf repetitiven Patterns)
4. Projekt-Priorisierung (was verdient mehr/weniger Zeit?)
5. Konkreter Verbesserungs-Challenge für die Woche
   (z.B. "Schaff es, deine Ø Focus-Session von 22min
   auf 30min zu steigern")

## Ideen-Sparring
Für die 2 Projekte mit dem meisten Zeitinvestment:
- 2-3 kreative, konkrete Ideen oder Denkanstöße
- Neue Features, neue Ansätze, neue Perspektiven
- Basierend auf dem was du aus den Fenstertiteln und
  Aktivitätsmustern über den Projektstand ablesen kannst

Schreibstil: Analytisch, strategisch, locker. Duze mich.
Sei ehrlich, auch wenn die Woche schlecht war.
```

---

### Task 3: Monthly Deep Dive

**Name:** WorkTracker Monthly Deep Dive
**Zeitplan:** 1. des Monats, 08:00

**Prompt:**

```
Du bist mein strategischer Produktivitäts-Berater für die
Monatsanalyse.

AUFGABE:
1. Lies die neueste .md Datei (nicht *-summary.md) aus
   ~/WorkTracker/summaries/monthly/
2. Lies alle *-summary.md aus ~/WorkTracker/summaries/weekly/
   die zu diesem Monat gehören
3. Lies die letzten 2 *-summary.md aus
   ~/WorkTracker/summaries/monthly/ zum Vergleich
4. Speichere als:
   ~/WorkTracker/summaries/monthly/[MONAT]-summary.md

INHALT:

## Monatsnarrative
Der Monat als Geschichte: Welche Projekte dominierten?
Wie hat sich der Fokus verschoben? Was war der rote Faden?
Gab es Wendepunkte? Wie hat sich die Arbeitsweise entwickelt?

## Monat in Zahlen
- Gesamte aktive Arbeitszeit
- Arbeitstage aktiv / Kalendertage
- Ø Arbeitszeit pro Tag
- Ø Start- und Endzeit
- Gesamte Focus-Time und Anteil
- Projekt-Verteilung (sortiert nach Zeit, mit Prozent-Balken)

## Wochenvergleich
| Woche | Zeit | Focus | Top-Projekt | Trend |
Markiere Trends und Ausreißer.

## Projektanalyse
Für jedes Projekt mit >2h im Monat:
- Zeitinvestment absolut und relativ
- Trend über die Wochen (steigend/fallend)
- Geschätzte Fortschritte basierend auf Aktivitätsmuster
- ROI-Einschätzung: Lohnt sich der Zeitaufwand?
- Empfehlung: Mehr/weniger/gleich viel Zeit investieren?
- Konkrete nächste Schritte

## Langzeit-Patterns
- Arbeitsgewohnheiten die sich verfestigt haben
- Was hat sich vs. Vormonat verändert?
- Gibt es problematische Trends?
  (wachsender Social-Media-Anteil, sinkende Focus-Time, etc.)
- Wie entwickelt sich die Produktivität insgesamt?

## Strategische Empfehlungen

### Arbeitsweise
- Was beibehalten? (mit Begründung aus den Daten)
- Was ändern? (mit konkretem Vorschlag)
- Welche Gewohnheiten bremsen? (mit Zahlen belegt)

### Automatisierungen
- Wiederkehrende manuelle Abläufe die automatisiert werden könnten
- Konkrete Vorschläge (Scripts, Tools, Workflows)
- Geschätzte monatliche Zeitersparnis

### Projektpriorisierung für nächsten Monat
- Ranking der Projekte nach empfohlener Priorität
- Begründung basierend auf Momentum, ROI und strategischem Wert
- Projekte die vernachlässigt wurden und Aufmerksamkeit brauchen

### Top 3 Ideen
Für die drei aktivsten Projekte jeweils eine konkrete,
innovative Idee basierend auf:
- Erkannten Stärken und Arbeitsmustern
- Lücken die aus der Aktivitätsanalyse sichtbar werden
- Trends in der Branche (basierend auf deinem Wissen)

## "Was wäre wenn"-Szenarien
Berechne/schätze basierend auf den realen Daten:
- "Wenn du [Kategorie X]-Zeit um 30% reduzierst
   → X Stunden/Monat mehr für Projekte"
- "Wenn Focus-Sessions auf Ø 45min steigen
   → geschätzt X% mehr Output"
- "Wenn du morgens mit Deep Work startest statt E-Mail/Chat
   → basierend auf deinen Daten wären Ø Xmin mehr Focus/Tag drin"
- "Wenn du [erkanntes Ablenkungsmuster] eliminierst
   → X Stunden/Monat gewonnen"

## Score-Karte
Bewerte den Monat auf einer Skala 1-10 in:
- Fokus
- Projektfortschritt
- Work-Life-Balance
- Effizienz
- Kreativität
Jeweils mit kurzer Begründung.

Schreibstil: Strategisch, direkt, datengetrieben.
Sei ein ehrlicher Berater, kein Ja-Sager.
Wenn der Monat schlecht war, sag es klar. Duze mich.
```

---

# ════════════════════════════════════════════
# STEP 7 — Testen & Iterieren
# Tool: Claude Code
# Dauer: Ongoing
# ════════════════════════════════════════════

## Prompt für Claude Code:

```
Ich möchte den WorkTracker end-to-end testen und validieren.

Prüfe folgendes:

1. COLLECTOR CHECK:
   - Läuft der Daemon? (launchctl list | grep worktracker)
   - Wird die heutige JSONL-Datei geschrieben?
   - Zeig mir die letzten 5 Zeilen der heutigen JSONL
   - Prüfe: Sind alle Felder befüllt? Gibt es null-Werte?
   - Dateigröße der heutigen JSONL — passt das zur erwarteten
     Größe (~500 Bytes × Snapshots)?
   - CPU/RAM Verbrauch des Collector-Prozesses?

2. AGGREGATOR CHECK:
   - Führe den Daily Aggregator manuell aus
   - Zeig mir den generierten Markdown
   - Prüfe: Sind die Sessions sinnvoll erkannt?
   - Prüfe: Stimmen die Projekt-Zuordnungen?
   - Prüfe: Sind die Berechnungen plausibel?

3. INTEGRATION CHECK:
   - Sind alle launchd-Jobs registriert und aktiv?
   - Stimmen die Zeitpläne?
   - Schreiben alle Komponenten in die richtigen Ordner?
   - Funktioniert das ctl.sh Script?

4. EDGE CASES:
   - Was passiert bei Mitternacht? (Tageswechsel in JSONL)
   - Was passiert wenn der Mac schläft und aufwacht?
   - Was passiert bei einem leeren Tag (keine Snapshots)?

Wenn du Probleme findest, fixe sie direkt.

Gib mir am Ende einen Status-Report:
- ✅ oder ❌ für jede Komponente
- Bekannte Einschränkungen
- Empfehlungen für Verbesserungen
```

---

## Zusammenfassung: Reihenfolge

| Step | Was | Tool | Wann |
|------|-----|------|------|
| 1 | Projekt-Setup | Claude Code | Sofort |
| 2 | Collector bauen | Claude Code | Sofort nach Step 1 |
| 3 | Collector als Daemon | Claude Code | Sofort nach Step 2 |
| 4 | Aggregator bauen | Claude Code | Nach 1-2 Tagen Daten |
| 5 | Aggregator-Jobs einrichten | Claude Code | Sofort nach Step 4 |
| 6 | Cowork Tasks anlegen | Claude Cowork | Sofort nach Step 5 |
| 7 | End-to-End Test | Claude Code | Nach Step 6 |

---

## Spätere Erweiterungen (Backlog)

- **Dashboard**: Lokale Web-App mit Echtzeit-Visualisierung
- **Screenshot-Analyse**: Periodische Screenshots mit Vision-AI
- **Keystroke-Inhalt**: Optional vollständige Eingaben tracken
- **Browser-History Integration**: URLs statt nur Fenstertitel
- **Git-Integration**: Commits und Branches mit Arbeitszeit korrelieren
- **Kalender-Integration**: Meetings vs. Deep Work analysieren
- **Notification-System**: Echtzeit-Warnung bei Ablenkung >15min
- **Pomodoro-Mode**: Automatische Focus-Timer basierend auf Patterns
