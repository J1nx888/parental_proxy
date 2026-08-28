"""common/matching.py: domain suffix matching, path matching, LAN check."""
from __future__ import annotations

import db
import matching


def _add_domain(conn, pattern, mode="splice", is_global=1, kind="generic"):
    conn.execute(
        "INSERT INTO domains (pattern, mode, kind, is_global, note, created_at) "
        "VALUES (?, ?, ?, ?, NULL, ?)",
        (pattern, mode, kind, is_global, db.now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM domains WHERE pattern = ?", (pattern,)).fetchone()


# ---------------------------------------------------------------- find_domain

def test_find_domain_exact_and_subdomain_match(conn):
    _add_domain(conn, r"crunchyroll\.com")
    assert matching.find_domain(conn, "crunchyroll.com") is not None
    assert matching.find_domain(conn, "www.crunchyroll.com") is not None
    assert matching.find_domain(conn, "a.b.crunchyroll.com") is not None


def test_find_domain_rejects_substring_prefix_bypass(conn):
    """S2.1: evil-jsdelivr.net must NOT match a jsdelivr\\.net rule."""
    _add_domain(conn, r"jsdelivr\.net")
    assert matching.find_domain(conn, "evil-jsdelivr.net") is None
    assert matching.find_domain(conn, "cdn.jsdelivr.net") is not None


def test_find_domain_rejects_suffix_appended_bypass(conn):
    """S2.1: crunchyroll.com.attacker.example must NOT match crunchyroll\\.com."""
    _add_domain(conn, r"crunchyroll\.com")
    assert matching.find_domain(conn, "crunchyroll.com.attacker.example") is None


def test_find_domain_is_case_insensitive(conn):
    _add_domain(conn, r"crunchyroll\.com")
    assert matching.find_domain(conn, "CRUNCHYROLL.COM") is not None
    assert matching.find_domain(conn, "WwW.Crunchyroll.CoM") is not None


def test_find_domain_strips_trailing_dot_and_whitespace(conn):
    _add_domain(conn, r"crunchyroll\.com")
    assert matching.find_domain(conn, "crunchyroll.com.") is not None
    assert matching.find_domain(conn, "  crunchyroll.com  ") is not None


def test_find_domain_no_match_returns_none(conn):
    _add_domain(conn, r"crunchyroll\.com")
    assert matching.find_domain(conn, "example.com") is None


def test_find_domain_empty_hostname_returns_none(conn):
    _add_domain(conn, r"crunchyroll\.com")
    assert matching.find_domain(conn, "") is None
    assert matching.find_domain(conn, None) is None  # type: ignore[arg-type]


def test_find_domain_first_match_wins_by_insertion_order(conn):
    row_a = _add_domain(conn, r"crunchyroll\.com", mode="splice")
    row_b = _add_domain(conn, r"[a-z]+\.com", mode="bump")  # broader, inserted later
    result = matching.find_domain(conn, "crunchyroll.com")
    assert result["id"] == row_a["id"]
    assert result["mode"] == "splice"
    # sanity: the broader pattern is reachable for hosts the first doesn't match
    assert matching.find_domain(conn, "example.com")["id"] == row_b["id"]


def test_find_domain_skips_invalid_regex_pattern_without_raising(conn):
    """A malformed pattern (e.g. an unbalanced group) must be skipped, not
    crash the whole lookup for every other row."""
    _add_domain(conn, r"(unbalanced", mode="splice")
    _add_domain(conn, r"crunchyroll\.com", mode="bump")
    result = matching.find_domain(conn, "crunchyroll.com")
    assert result is not None
    assert result["mode"] == "bump"


# ---------------------------------------------------------------- path_allowed

def test_path_allowed_no_rules_returns_false(conn):
    domain = _add_domain(conn, r"example\.com", mode="bump")
    assert matching.path_allowed(conn, domain["id"], "/anything") is False


def test_path_allowed_matches_configured_pattern(conn):
    domain = _add_domain(conn, r"example\.com", mode="bump")
    conn.execute(
        "INSERT INTO domain_paths (domain_id, pattern) VALUES (?, ?)",
        (domain["id"], r"^/discover"),
    )
    conn.commit()
    assert matching.path_allowed(conn, domain["id"], "/discover/seasonal") is True
    assert matching.path_allowed(conn, domain["id"], "/other") is False


def test_path_allowed_defaults_path_to_slash(conn):
    domain = _add_domain(conn, r"example\.com", mode="bump")
    conn.execute(
        "INSERT INTO domain_paths (domain_id, pattern) VALUES (?, ?)", (domain["id"], r"^/$")
    )
    conn.commit()
    assert matching.path_allowed(conn, domain["id"], "") is True
    assert matching.path_allowed(conn, domain["id"], None) is True


def test_path_allowed_skips_invalid_regex_pattern(conn):
    domain = _add_domain(conn, r"example\.com", mode="bump")
    conn.execute(
        "INSERT INTO domain_paths (domain_id, pattern) VALUES (?, ?)", (domain["id"], r"(unbalanced")
    )
    conn.commit()
    assert matching.path_allowed(conn, domain["id"], "/anything") is False


# ---------------------------------------------------------- ip_in_configured_lan

def test_ip_in_configured_lan_disabled_when_blank(conn):
    # no 'local_network' setting at all
    assert matching.ip_in_configured_lan(conn, "8.8.8.8") is True
    db.set_setting(conn, "local_network", "   ")
    conn.commit()
    assert matching.ip_in_configured_lan(conn, "8.8.8.8") is True


def test_ip_in_configured_lan_checks_cidr(conn):
    db.set_setting(conn, "local_network", "192.168.1.0/24")
    conn.commit()
    assert matching.ip_in_configured_lan(conn, "192.168.1.42") is True
    assert matching.ip_in_configured_lan(conn, "10.0.0.1") is False


def test_ip_in_configured_lan_multiple_cidrs(conn):
    db.set_setting(conn, "local_network", "192.168.1.0/24 10.0.0.0/8")
    conn.commit()
    assert matching.ip_in_configured_lan(conn, "192.168.1.5") is True
    assert matching.ip_in_configured_lan(conn, "10.5.5.5") is True
    assert matching.ip_in_configured_lan(conn, "172.16.0.1") is False


def test_ip_in_configured_lan_bad_cidr_entry_is_skipped(conn):
    db.set_setting(conn, "local_network", "not-a-cidr 192.168.1.0/24")
    conn.commit()
    assert matching.ip_in_configured_lan(conn, "192.168.1.5") is True
    assert matching.ip_in_configured_lan(conn, "10.0.0.1") is False


def test_ip_in_configured_lan_bad_client_ip_fails_closed(conn):
    db.set_setting(conn, "local_network", "192.168.1.0/24")
    conn.commit()
    assert matching.ip_in_configured_lan(conn, "not-an-ip") is False


def test_ip_in_configured_lan_ipv6(conn):
    db.set_setting(conn, "local_network", "fd00::/8")
    conn.commit()
    assert matching.ip_in_configured_lan(conn, "fd00::1") is True
    assert matching.ip_in_configured_lan(conn, "2001:db8::1") is False


def test_ip_in_configured_lan_ipv4_cidr_does_not_match_ipv6(conn):
    db.set_setting(conn, "local_network", "192.168.1.0/24")
    conn.commit()
    assert matching.ip_in_configured_lan(conn, "::1") is False


# --------------------------------------------------------------- user helpers

def test_user_has_domain_and_get_user_by_username(conn):
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) "
        "VALUES ('kid1', 'Kid One', 'x', ?)",
        (db.now_iso(),),
    )
    conn.commit()
    user = matching.get_user_by_username(conn, "kid1")
    assert user is not None
    assert matching.get_user_by_username(conn, "nope") is None

    domain = _add_domain(conn, r"example\.com", is_global=0)
    assert matching.user_has_domain(conn, user["id"], domain["id"]) is False
    conn.execute(
        "INSERT INTO user_domains (user_id, domain_id) VALUES (?, ?)", (user["id"], domain["id"])
    )
    conn.commit()
    assert matching.user_has_domain(conn, user["id"], domain["id"]) is True


def test_user_has_show_is_case_insensitive_on_series_id(conn):
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) "
        "VALUES ('kid1', 'Kid One', 'x', ?)",
        (db.now_iso(),),
    )
    conn.commit()
    user = matching.get_user_by_username(conn, "kid1")
    conn.execute(
        "INSERT INTO user_shows (user_id, series_id, series_name) VALUES (?, 'GYE5K0XVR', 'Ace Attorney')",
        (user["id"],),
    )
    conn.commit()
    assert matching.user_has_show(conn, user["id"], "gye5k0xvr") is True
    assert matching.user_has_show(conn, user["id"], "OTHERID") is False
