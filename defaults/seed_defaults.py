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
    ("crunchyrollcdn\\.com", "Crunchyroll CDN"),
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
    cr_row = conn.execute(
        "SELECT id FROM domains WHERE pattern = 'crunchyroll\\.com'"
    ).fetchone()
    if cr_row:
        for pattern in CRUNCHYROLL_PATHS:
            conn.execute(
                "INSERT OR IGNORE INTO domain_paths (domain_id, pattern) VALUES (?, ?)",
                (cr_row["id"], pattern),
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
