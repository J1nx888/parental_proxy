#!/usr/bin/env python3
"""Seed the database with sane defaults on first run. Idempotent: every
insert uses INSERT OR IGNORE / set_setting_if_absent, so re-running this
against an already-configured database changes nothing. Anything the admin
has since edited or removed via the dashboard stays as they left it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db

# Infrastructure Crunchyroll's site depends on -- global, splice mode (never
# decrypted, just a host-level pass-through once allowed). Carried over from
# v1's allowed_sites.txt.
GLOBAL_SPLICE_DOMAINS = [
    ("google\\.com", "Google"),
    ("gstatic\\.com", "Google static assets"),
    ("googleapis\\.com", "Google APIs"),
    ("googleusercontent\\.com", "Google user content"),
    ("ctfassets\\.net", "Crunchyroll CMS assets"),
    ("vimeo\\.com", "Video embeds"),
    ("segment\\.com", "Analytics"),
    ("braze\\.com", "Notifications"),
    ("akamaized\\.net", "CDN"),
    ("auth0\\.com", "Single sign-on"),
    ("firebaseapp\\.com", "Single sign-on"),
    ("ipify\\.org", "Geolocation"),
    ("ipapi\\.co", "Geolocation"),
    ("iplocate\\.io", "Geolocation"),
    ("ipinfo\\.io", "Geolocation"),
    ("bitmovin\\.com", "Video player"),
    ("litix\\.io", "Video player telemetry"),
    ("cookielaw\\.org", "Cookie consent"),
    ("ketchcdn\\.com", "Cookie consent"),
    ("ketchjs\\.com", "Cookie consent"),
    ("jsdelivr\\.net", "Script CDN"),
    ("onetrust\\.com", "Cookie consent"),
    ("datadoghq\\.com", "Telemetry"),
    ("googletagmanager\\.com", "Telemetry"),
]

# Always spliced, never checked or logged -- large binary CDN traffic where
# there's nothing meaningful to authorize per-user (the show-level decision
# already happened at the manifest/playback-token request, which IS bumped).
TRUSTED_DOMAINS = [
    ("gccrunchyroll\\.com", "Crunchyroll raw video CDN"),
    ("crunchyrollcdn\\.com", "Crunchyroll raw video CDN"),
]

# Crunchyroll's own paths -- defense-in-depth for request shapes the
# classifier doesn't specifically recognize. Carried over from v1's
# allowed_paths.txt.
CRUNCHYROLL_PATHS = [
    r"^/$", r"^/\?",
    r"^/login", r"^/auth", r"^/api",
    r"^/simulcastcalendar", r"^/news",
    r"^/assets", r"^/browser", r"^/build/", r"^/config", r"^/cdn",
    r"^/assets/", r"^/css/", r"^/js/", r"^/images/",
    r"\.(css|js|png|jpg|jpeg|gif|svg|webp|json|woff2?)$",
    r"^/discover",
    r"^/config-delta/",
    r"^/subs/v[0-9]+/",
    r"^/playback/v[0-9]+/",
    r"^/callback",
    r"^/accounts/v[0-9]+/",
    r"^/f/v[0-9]+/",
    r"^/content/v[0-9]+/",
    r"^/i18n/", r"^/skip-events/",
    r"^/content/v[0-9]+/.*playheads",
    r"^/v1/track", r"^/v1/p$",
    r"/content-reviews/",
    r"/v1/",
    # "Top 10"-style recommendation rows on the discover/home page (GH #4).
    r"^/personalization/v[0-9]+/",
]


# Phase 8 starter categories. URLs are all from The Block List Project
# (https://github.com/blocklistproject/Lists, MIT, actively maintained) --
# confirmed LIVE 2026-08-31/09-01 (not assumed from its README alone): each
# fetched, format-checked (AdGuard/adblock rule syntax, matching
# common/blocklist_parser.py), and entry-counted. Counts shift as the
# upstream lists update; the ones noted below are what was true when this
# was written, kept only to explain the is_global/scoped split the
# dashboard's category routes actually enforce
# (matching.MAX_SCOPED_CATEGORY_DOMAINS = 5000):
#   - Porn (953,393), Gambling (278,856), Drugs (26,029), Fraud (256,268),
#     Facebook (22,362) are all already over the threshold -- Everyone-only,
#     regardless of what an admin later tries to scope them to.
#   - TikTok (3,725), Twitter/X (1,193), WhatsApp (226) are small enough to
#     scope to a specific kid/device if wanted.
# None are seeded `is_global` by default -- an admin has to actually decide
# to turn a category on (and for whom) from the Categories page; seeding
# the row alone blocks nothing. None have any `category_domains` rows yet
# either -- that only happens once something calls
# `common/category_fetch.py`'s `fetch_and_sync_category()` (the
# controller's own daily background loop, or the dashboard's "Sync now"
# button), same as a freshly-seeded row with no data until its first real
# fetch.
#
# No public blocklist exists for "AI" or "Weapons" (confirmed via research
# the same session) -- both seeded with subscription_url=None,
# manual-curation-only, ready for an admin (or a future pass) to add
# domains to directly from the category's Manage page.
_BLOCKLISTPROJECT_ADGUARD = "https://blocklistproject.github.io/Lists/adguard/{}-ags.txt"

DEFAULT_CATEGORIES = [
    ("Adult", _BLOCKLISTPROJECT_ADGUARD.format("porn")),
    ("Gambling", _BLOCKLISTPROJECT_ADGUARD.format("gambling")),
    ("Drugs", _BLOCKLISTPROJECT_ADGUARD.format("drugs")),
    ("Fraud & Scams", _BLOCKLISTPROJECT_ADGUARD.format("fraud")),
    ("Facebook", _BLOCKLISTPROJECT_ADGUARD.format("facebook")),
    ("TikTok", _BLOCKLISTPROJECT_ADGUARD.format("tiktok")),
    ("Twitter/X", _BLOCKLISTPROJECT_ADGUARD.format("twitter")),
    ("WhatsApp", _BLOCKLISTPROJECT_ADGUARD.format("whatsapp")),
    ("AI", None),
    ("Weapons", None),
]


def seed(conn) -> None:
    for pattern, note in GLOBAL_SPLICE_DOMAINS:
        conn.execute(
            "INSERT OR IGNORE INTO domains (pattern, mode, kind, is_global, note, created_at) "
            "VALUES (?, 'splice', 'generic', 1, ?, ?)",
            (pattern, note, db.now_iso()),
        )

    for pattern, note in TRUSTED_DOMAINS:
        conn.execute(
            "INSERT OR IGNORE INTO domains (pattern, mode, kind, is_global, note, created_at) "
            "VALUES (?, 'trusted', 'generic', 1, ?, ?)",
            (pattern, note, db.now_iso()),
        )

    conn.execute(
        "INSERT OR IGNORE INTO domains (pattern, mode, kind, is_global, note, created_at) "
        "VALUES ('crunchyroll\\.com', 'bump', 'crunchyroll', 1, 'Crunchyroll -- shows approved per-user', ?)",
        (db.now_iso(),),
    )
    # No separate crunchyrollsvc.com playback-service domain: confirmed
    # 2026-08-28 against Crunchyroll's own live webpack bundle that
    # production playback is served from www.crunchyroll.com/playback (the
    # cr-play-service.*.crunchyrollsvc.com host is explicitly dev-only in
    # Crunchyroll's own config, never used by real traffic) -- already
    # covered by the crunchyroll.com domain above. See
    # docs/review-2026-08-28.md item 1.2.
    cr_row = conn.execute(
        "SELECT id FROM domains WHERE pattern = 'crunchyroll\\.com'"
    ).fetchone()
    if cr_row:
        for pattern in CRUNCHYROLL_PATHS:
            conn.execute(
                "INSERT OR IGNORE INTO domain_paths (domain_id, pattern) VALUES (?, ?)",
                (cr_row["id"], pattern),
            )

    for name, subscription_url in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, subscription_url, is_global, created_at) "
            "VALUES (?, ?, 0, ?)",
            (name, subscription_url, db.now_iso()),
        )


def main() -> int:
    conn = db.get_conn()
    db.init_db(conn)
    seed(conn)
    conn.commit()
    conn.close()
    print("Seed complete (idempotent -- existing data untouched).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
