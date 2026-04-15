#!/usr/bin/env python3
"""WorkTracker Web Categories — automatic URL classification for browsing analysis.

Provides a domain-based knowledge base and classification function that maps
browser URLs to a hierarchical category tree (main_category → subcategory → domain).
No external dependencies — only stdlib.
"""

from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Domain → (main_category, subcategory) knowledge base
# ---------------------------------------------------------------------------

DOMAIN_CATEGORIES: dict[str, tuple[str, str]] = {
    # ── Development ────────────────────────────────────────────
    "github.com":            ("Development", "Code Hosting"),
    "gitlab.com":            ("Development", "Code Hosting"),
    "bitbucket.org":         ("Development", "Code Hosting"),
    "codeberg.org":          ("Development", "Code Hosting"),
    "sourcehut.org":         ("Development", "Code Hosting"),
    "stackoverflow.com":     ("Development", "Q&A"),
    "stackexchange.com":     ("Development", "Q&A"),
    "superuser.com":         ("Development", "Q&A"),
    "serverfault.com":       ("Development", "Q&A"),
    "askubuntu.com":         ("Development", "Q&A"),
    "npmjs.com":             ("Development", "Packages"),
    "pypi.org":              ("Development", "Packages"),
    "crates.io":             ("Development", "Packages"),
    "rubygems.org":          ("Development", "Packages"),
    "packagist.org":         ("Development", "Packages"),
    "cocoapods.org":         ("Development", "Packages"),
    "developer.apple.com":   ("Development", "Documentation"),
    "devdocs.io":            ("Development", "Documentation"),
    "readthedocs.io":        ("Development", "Documentation"),
    "readthedocs.org":       ("Development", "Documentation"),
    "docs.python.org":       ("Development", "Documentation"),
    "developer.mozilla.org": ("Development", "Documentation"),
    "docs.rs":               ("Development", "Documentation"),
    "docs.github.com":       ("Development", "Documentation"),
    "learn.microsoft.com":   ("Development", "Documentation"),
    "mongodb.com":           ("Development", "Documentation"),
    "docker.com":            ("Development", "DevOps"),
    "hub.docker.com":        ("Development", "DevOps"),
    "vercel.com":            ("Development", "DevOps"),
    "netlify.com":           ("Development", "DevOps"),
    "heroku.com":            ("Development", "DevOps"),
    "digitalocean.com":      ("Development", "DevOps"),
    "aws.amazon.com":        ("Development", "DevOps"),
    "cloud.google.com":      ("Development", "DevOps"),
    "portal.azure.com":      ("Development", "DevOps"),
    "codepen.io":            ("Development", "Playground"),
    "jsfiddle.net":          ("Development", "Playground"),
    "codesandbox.io":        ("Development", "Playground"),
    "replit.com":            ("Development", "Playground"),

    # ── AI ─────────────────────────────────────────────────────
    "claude.ai":             ("AI", "Chatbots"),
    "claude.com":            ("AI", "Chatbots"),
    "chatgpt.com":           ("AI", "Chatbots"),
    "chat.openai.com":       ("AI", "Chatbots"),
    "gemini.google.com":     ("AI", "Chatbots"),
    "grok.com":              ("AI", "Chatbots"),
    "x.ai":                  ("AI", "Chatbots"),
    "poe.com":               ("AI", "Chatbots"),
    "perplexity.ai":         ("AI", "Chatbots"),
    "anthropic.com":         ("AI", "Platforms"),
    "openai.com":            ("AI", "Platforms"),
    "deepmind.google":       ("AI", "Platforms"),
    "openrouter.ai":         ("AI", "Tools"),
    "elevenlabs.io":         ("AI", "Tools"),
    "bfl.ai":                ("AI", "Tools"),
    "aistudio.google.com":   ("AI", "Tools"),
    "huggingface.co":        ("AI", "Tools"),
    "replicate.com":         ("AI", "Tools"),
    "midjourney.com":        ("AI", "Tools"),
    "stability.ai":          ("AI", "Tools"),
    "runwayml.com":          ("AI", "Tools"),
    "suno.com":              ("AI", "Tools"),
    "udio.com":              ("AI", "Tools"),

    # ── Social Media ───────────────────────────────────────────
    "reddit.com":            ("Social Media", "Reddit"),
    "old.reddit.com":        ("Social Media", "Reddit"),
    "instagram.com":         ("Social Media", "Instagram"),
    "twitter.com":           ("Social Media", "Twitter/X"),
    "x.com":                 ("Social Media", "Twitter/X"),
    "facebook.com":          ("Social Media", "Facebook"),
    "tiktok.com":            ("Social Media", "TikTok"),
    "linkedin.com":          ("Social Media", "LinkedIn"),
    "threads.net":           ("Social Media", "Threads"),
    "mastodon.social":       ("Social Media", "Mastodon"),
    "bsky.app":              ("Social Media", "Bluesky"),
    "tumblr.com":            ("Social Media", "Tumblr"),
    "pinterest.com":         ("Social Media", "Pinterest"),

    # ── News ───────────────────────────────────────────────────
    "orf.at":                ("News", "Austrian"),
    "derstandard.at":        ("News", "Austrian"),
    "diepresse.com":         ("News", "Austrian"),
    "kurier.at":             ("News", "Austrian"),
    "krone.at":              ("News", "Austrian"),
    "heute.at":              ("News", "Austrian"),
    "vienna.at":             ("News", "Austrian"),
    "kleinezeitung.at":      ("News", "Austrian"),
    "salzburg24.at":         ("News", "Austrian"),
    "tt.com":                ("News", "Austrian"),
    "nachrichten.at":        ("News", "Austrian"),
    "vol.at":                ("News", "Austrian"),
    "bbc.com":               ("News", "International"),
    "bbc.co.uk":             ("News", "International"),
    "reuters.com":           ("News", "International"),
    "theguardian.com":       ("News", "International"),
    "nytimes.com":           ("News", "International"),
    "washingtonpost.com":    ("News", "International"),
    "cnn.com":               ("News", "International"),
    "aljazeera.com":         ("News", "International"),
    "spiegel.de":            ("News", "German"),
    "zeit.de":               ("News", "German"),
    "sueddeutsche.de":       ("News", "German"),
    "faz.net":               ("News", "German"),
    "tagesschau.de":         ("News", "German"),
    "heise.de":              ("News", "Tech"),
    "techcrunch.com":        ("News", "Tech"),
    "theverge.com":          ("News", "Tech"),
    "arstechnica.com":       ("News", "Tech"),
    "wired.com":             ("News", "Tech"),
    "hackernews.com":        ("News", "Tech"),
    "news.ycombinator.com":  ("News", "Tech"),

    # ── Entertainment ──────────────────────────────────────────
    "youtube.com":           ("Entertainment", "Video"),
    "netflix.com":           ("Entertainment", "Video"),
    "twitch.tv":             ("Entertainment", "Video"),
    "disneyplus.com":        ("Entertainment", "Video"),
    "primevideo.com":        ("Entertainment", "Video"),
    "vimeo.com":             ("Entertainment", "Video"),
    "dailymotion.com":       ("Entertainment", "Video"),
    "on.orf.at":             ("Entertainment", "Video"),
    "spotify.com":           ("Entertainment", "Music"),
    "soundcloud.com":        ("Entertainment", "Music"),
    "music.youtube.com":     ("Entertainment", "Music"),
    "music.apple.com":       ("Entertainment", "Music"),
    "tidal.com":             ("Entertainment", "Music"),
    "bandcamp.com":          ("Entertainment", "Music"),
    "deezer.com":            ("Entertainment", "Music"),
    "store.steampowered.com":("Entertainment", "Gaming"),
    "epicgames.com":         ("Entertainment", "Gaming"),
    "gog.com":               ("Entertainment", "Gaming"),
    "twitch.tv":             ("Entertainment", "Gaming"),
    "win2day.at":            ("Entertainment", "Gambling"),
    "bet365.com":            ("Entertainment", "Gambling"),
    "imdb.com":              ("Entertainment", "Reference"),
    "rottentomatoes.com":    ("Entertainment", "Reference"),
    "letterboxd.com":        ("Entertainment", "Reference"),

    # ── Finance ────────────────────────────────────────────────
    "binance.com":           ("Finance", "Crypto"),
    "coingecko.com":         ("Finance", "Crypto"),
    "coinbase.com":          ("Finance", "Crypto"),
    "coinmarketcap.com":     ("Finance", "Crypto"),
    "cryptobubbles.net":     ("Finance", "Crypto"),
    "etherscan.io":          ("Finance", "Crypto"),
    "tradingview.com":       ("Finance", "Trading"),
    "traderepublic.com":     ("Finance", "Stocks"),
    "scalable.capital":      ("Finance", "Stocks"),
    "flatex.at":             ("Finance", "Stocks"),
    "bank99.at":             ("Finance", "Banking"),
    "george.at":             ("Finance", "Banking"),
    "sparkasse.at":          ("Finance", "Banking"),
    "raiffeisen.at":         ("Finance", "Banking"),
    "easybank.at":           ("Finance", "Banking"),
    "kalshi.com":            ("Finance", "Prediction Markets"),
    "polymarket.com":        ("Finance", "Prediction Markets"),
    "metaculus.com":         ("Finance", "Prediction Markets"),
    "bitget.com":            ("Finance", "Crypto"),

    # ── Communication ──────────────────────────────────────────
    "gmail.com":             ("Communication", "Email"),
    "mail.google.com":       ("Communication", "Email"),
    "outlook.live.com":      ("Communication", "Email"),
    "outlook.office.com":    ("Communication", "Email"),
    "proton.me":             ("Communication", "Email"),
    "protonmail.com":        ("Communication", "Email"),
    "web.whatsapp.com":      ("Communication", "Chat"),
    "slack.com":             ("Communication", "Chat"),
    "web.telegram.org":      ("Communication", "Chat"),
    "discord.com":           ("Communication", "Chat"),
    "meet.google.com":       ("Communication", "Video Calls"),
    "zoom.us":               ("Communication", "Video Calls"),
    "teams.microsoft.com":   ("Communication", "Video Calls"),

    # ── Research ───────────────────────────────────────────────
    "google.com":            ("Research", "Search"),
    "google.at":             ("Research", "Search"),
    "duckduckgo.com":        ("Research", "Search"),
    "bing.com":              ("Research", "Search"),
    "ecosia.org":            ("Research", "Search"),
    "startpage.com":         ("Research", "Search"),
    "wikipedia.org":         ("Research", "Wikipedia"),
    "de.wikipedia.org":      ("Research", "Wikipedia"),
    "en.wikipedia.org":      ("Research", "Wikipedia"),
    "scholar.google.com":    ("Research", "Academic"),
    "arxiv.org":             ("Research", "Academic"),
    "researchgate.net":      ("Research", "Academic"),
    "translate.google.com":  ("Research", "Translation"),
    "deepl.com":             ("Research", "Translation"),
    "dict.cc":               ("Research", "Translation"),
    "leo.org":               ("Research", "Translation"),

    # ── Shopping ───────────────────────────────────────────────
    "amazon.de":             ("Shopping", "Amazon"),
    "amazon.at":             ("Shopping", "Amazon"),
    "amazon.com":            ("Shopping", "Amazon"),
    "ebay.at":               ("Shopping", "eBay"),
    "ebay.de":               ("Shopping", "eBay"),
    "geizhals.at":           ("Shopping", "Preisvergleich"),
    "geizhals.de":           ("Shopping", "Preisvergleich"),
    "idealo.at":             ("Shopping", "Preisvergleich"),
    "willhaben.at":          ("Shopping", "Kleinanzeigen"),
    "shpock.com":            ("Shopping", "Kleinanzeigen"),
    "zalando.at":            ("Shopping", "Fashion"),
    "aboutyou.at":           ("Shopping", "Fashion"),
    "aliexpress.com":        ("Shopping", "International"),
    "etsy.com":              ("Shopping", "Handmade"),

    # ── Productivity ───────────────────────────────────────────
    "docs.google.com":       ("Productivity", "Documents"),
    "sheets.google.com":     ("Productivity", "Spreadsheets"),
    "slides.google.com":     ("Productivity", "Presentations"),
    "drive.google.com":      ("Productivity", "Cloud Storage"),
    "dropbox.com":           ("Productivity", "Cloud Storage"),
    "notion.so":             ("Productivity", "Notes"),
    "obsidian.md":           ("Productivity", "Notes"),
    "trello.com":            ("Productivity", "Project Management"),
    "asana.com":             ("Productivity", "Project Management"),
    "linear.app":            ("Productivity", "Project Management"),
    "jira.atlassian.com":    ("Productivity", "Project Management"),
    "figma.com":             ("Productivity", "Design"),
    "canva.com":             ("Productivity", "Design"),

    # ── Government & Services ──────────────────────────────────
    "oesterreich.gv.at":     ("Government", "Austrian"),
    "finanzonline.bmf.gv.at":("Government", "Austrian"),
    "sozialversicherung.at": ("Government", "Austrian"),
    "ris.bka.gv.at":         ("Government", "Austrian"),
    "linz.at":               ("Government", "Municipal"),
    "wien.gv.at":            ("Government", "Municipal"),

    # ── Travel ─────────────────────────────────────────────────
    "booking.com":           ("Travel", "Hotels"),
    "airbnb.com":            ("Travel", "Hotels"),
    "maps.google.com":       ("Travel", "Maps"),
    "maps.apple.com":        ("Travel", "Maps"),
    "flightradar24.com":     ("Travel", "Flights"),
    "skyscanner.at":         ("Travel", "Flights"),
    "oebb.at":               ("Travel", "Transport"),
    "wienerlinien.at":       ("Travel", "Transport"),
}


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------


def classify_url(url: str) -> tuple[str, str]:
    """Classify a URL into (main_category, subcategory).

    Strategy:
    1. Extract domain, strip 'www.'
    2. Exact lookup in DOMAIN_CATEGORIES
    3. Parent-domain fallback (e.g. on.orf.at → orf.at)
    4. Fallback: ("Other", "Uncategorized")
    """
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ("Other", "Uncategorized")

    if not netloc:
        return ("Other", "Uncategorized")

    # Strip www.
    if netloc.startswith("www."):
        netloc = netloc[4:]

    # Strip port
    if ":" in netloc:
        netloc = netloc.split(":")[0]

    # Localhost / local development
    if netloc in ("localhost", "127.0.0.1", "0.0.0.0", "[::]", "[::1]"):
        return ("Development", "Local")

    # Exact match
    result = DOMAIN_CATEGORIES.get(netloc)
    if result:
        return result

    # Parent-domain fallback: try stripping subdomains one level at a time
    parts = netloc.split(".")
    while len(parts) > 2:
        parts = parts[1:]
        parent = ".".join(parts)
        result = DOMAIN_CATEGORIES.get(parent)
        if result:
            return result

    # Special case for two-part TLDs (co.uk, gv.at, or.at, co.at)
    # e.g. "news.bbc.co.uk" → try "bbc.co.uk"
    if len(parts) >= 3 and parts[-2] in ("co", "or", "gv", "ac", "org"):
        parent = ".".join(parts[-3:])
        result = DOMAIN_CATEGORIES.get(parent)
        if result:
            return result

    return ("Other", "Uncategorized")


def _extract_domain(url: str) -> str:
    """Extract clean domain from URL (strips www. and port)."""
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return "unknown"
    if not netloc:
        return "unknown"
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if ":" in netloc:
        netloc = netloc.split(":")[0]
    return netloc or "unknown"


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------


def build_web_category_tree(sessions: list[dict]) -> list[dict]:
    """Build a 3-level category tree from browser sessions.

    Returns a sorted list of main categories, each containing subcategories,
    each containing domains with time in seconds.

    Percentages are relative to total browser time (sessions with URLs).
    """
    # Accumulate: main_cat → sub_cat → domain → seconds
    tree: dict[str, dict[str, dict[str, float]]] = {}
    total_browser_sec = 0.0

    for s in sessions:
        if s.get("app_category") != "Browser":
            continue
        url = s.get("url", "")
        if not url:
            continue
        dur = s.get("duration_seconds", 0)
        if dur <= 0:
            continue

        total_browser_sec += dur
        main_cat, sub_cat = classify_url(url)
        domain = _extract_domain(url)

        if main_cat not in tree:
            tree[main_cat] = {}
        if sub_cat not in tree[main_cat]:
            tree[main_cat][sub_cat] = {}
        tree[main_cat][sub_cat][domain] = tree[main_cat][sub_cat].get(domain, 0) + dur

    if total_browser_sec == 0:
        return []

    # Build sorted output
    result = []
    for main_cat, subcats in sorted(tree.items(),
                                     key=lambda x: sum(sum(d.values()) for d in x[1].values()),
                                     reverse=True):
        main_sec = sum(sum(d.values()) for d in subcats.values())
        sub_list = []
        for sub_cat, domains in sorted(subcats.items(),
                                        key=lambda x: sum(x[1].values()),
                                        reverse=True):
            sub_sec = sum(domains.values())
            domain_list = [
                {
                    "domain": dom,
                    "sec": round(sec),
                    "pct": round(sec / total_browser_sec * 100, 1),
                }
                for dom, sec in sorted(domains.items(), key=lambda x: x[1], reverse=True)
            ]
            sub_list.append({
                "name": sub_cat,
                "sec": round(sub_sec),
                "pct": round(sub_sec / total_browser_sec * 100, 1),
                "domains": domain_list,
            })
        result.append({
            "name": main_cat,
            "sec": round(main_sec),
            "pct": round(main_sec / total_browser_sec * 100, 1),
            "subcategories": sub_list,
        })

    return result
