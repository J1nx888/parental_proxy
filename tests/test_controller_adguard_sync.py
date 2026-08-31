"""controller/adguard_sync.py: the hard-deny rule builder, the managed-block
merge logic, and the periodic sync loop. Network access always goes through
adguard_client, which every test here fakes -- consistent with
test_controller_discovery.py not shelling out to a real `ip neigh show`.
"""
from __future__ import annotations

import threading
import time

import pytest

import adguard_sync


def _insert_domain(conn, pattern: str, mode: str = "bump", is_global: bool = True) -> int:
    conn.execute(
        "INSERT INTO domains (pattern, mode, kind, is_global, created_at) "
        "VALUES (?, ?, 'generic', ?, datetime('now'))",
        (pattern, mode, int(is_global)),
    )
    conn.commit()
    return conn.execute("SELECT id FROM domains WHERE pattern = ?", (pattern,)).fetchone()["id"]


def _insert_device_with_binding(
    conn, mac: str, ip: str, *, bump_enabled: bool = False, active: bool = True,
    is_authenticated: bool = True, user_id: int | None = None, group_id: int | None = None,
    ignored: bool = False,
) -> int:
    conn.execute(
        "INSERT INTO devices (mac_address, bump_enabled, is_authenticated, user_id, group_id, ignored, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
        (mac, int(bump_enabled), int(is_authenticated), user_id, group_id, int(ignored)),
    )
    device_id = conn.execute("SELECT id FROM devices WHERE mac_address = ?", (mac,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO device_bindings "
        "(device_id, mac_address, ipv4_address, first_seen_at, last_seen_at, source, active) "
        "VALUES (?, ?, ?, datetime('now'), datetime('now'), 'active_scan', ?)",
        (device_id, mac, ip, int(active)),
    )
    conn.commit()
    return device_id


def _insert_group(conn, name: str) -> int:
    conn.execute("INSERT INTO groups (name, created_at) VALUES (?, datetime('now'))", (name,))
    conn.commit()
    return conn.execute("SELECT id FROM groups WHERE name = ?", (name,)).fetchone()["id"]


# ============================================================
# _domain_rule
# ============================================================

def test_domain_rule_shape_mirrors_matching_pys_own_anchoring():
    rule = adguard_sync._domain_rule("crunchyroll\\.com", ["192.168.1.10"])
    assert rule == "/(?i)(?:^|\\.)(?:crunchyroll\\.com)$/$client=192.168.1.10"


def test_domain_rule_joins_multiple_client_ips_with_commas():
    rule = adguard_sync._domain_rule("example\\.com", ["10.0.0.1", "10.0.0.2"])
    assert rule.endswith("$client=10.0.0.1,10.0.0.2")


def test_domain_rule_without_block_page_ip_has_no_dnsrewrite():
    rule = adguard_sync._domain_rule("example\\.com", ["10.0.0.1"])
    assert "dnsrewrite" not in rule


def test_domain_rule_with_block_page_ip_adds_dnsrewrite():
    rule = adguard_sync._domain_rule("example\\.com", ["10.0.0.1"], block_page_ip="192.168.1.50")
    assert rule == "/(?i)(?:^|\\.)(?:example\\.com)$/$client=10.0.0.1,dnsrewrite=NOERROR;A;192.168.1.50"


# ============================================================
# build_rules
# ============================================================

def test_build_rules_empty_when_no_bump_domains(conn):
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False)
    assert adguard_sync.build_rules(conn) == []


def test_build_rules_empty_when_no_non_bump_device_has_an_ip(conn):
    _insert_domain(conn, "crunchyroll\\.com")
    # Only a bump_enabled device is bound -- nothing to deny it from.
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=True)
    assert adguard_sync.build_rules(conn) == []


def test_build_rules_covers_every_non_bump_device(conn):
    _insert_domain(conn, "crunchyroll\\.com")
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False)
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:02", "192.168.1.11", bump_enabled=False)
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:03", "192.168.1.12", bump_enabled=True)

    rules = adguard_sync.build_rules(conn)

    assert len(rules) == 1
    assert "192.168.1.10" in rules[0]
    assert "192.168.1.11" in rules[0]
    assert "192.168.1.12" not in rules[0], "a bump_enabled device must never be added to the deny list"


def test_build_rules_still_denies_a_bump_enabled_device_that_is_not_yet_authenticated(conn):
    """Regression test for a real gap found and fixed 2026-08-31 (same
    class of bug as classify_device()/bypass_login -- see RoadMap.md):
    build_rules() used to select on the raw bump_enabled column alone,
    so a device with bump_enabled=1 but is_authenticated=0 (a genuinely
    new PREAUTH device, or one pre-configured for bump ahead of its
    first login) was excluded from the hard-deny list -- while ALSO not
    being nftables bump_v4-eligible (bump_eligible() requires
    AUTHENTICATED too) -- a full, unfiltered bypass of the hard-deny
    invariant this module exists to enforce. Must now be denied, same
    as any other non-bump-eligible device, until it actually logs in."""
    _insert_domain(conn, "crunchyroll\\.com")
    _insert_device_with_binding(
        conn, "aa:bb:cc:dd:ee:04", "192.168.1.13", bump_enabled=True, is_authenticated=False
    )

    rules = adguard_sync.build_rules(conn)

    assert len(rules) == 1
    assert "192.168.1.13" in rules[0]


def test_build_rules_threads_block_page_ip_into_every_rule(conn):
    _insert_domain(conn, "crunchyroll\\.com")
    _insert_domain(conn, "example\\.com")
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False)

    rules = adguard_sync.build_rules(conn, block_page_ip="192.168.1.50")

    assert len(rules) == 2
    assert all("dnsrewrite=NOERROR;A;192.168.1.50" in r for r in rules)


def test_build_rules_ignores_inactive_bindings(conn):
    _insert_domain(conn, "crunchyroll\\.com")
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False, active=False)
    assert adguard_sync.build_rules(conn) == [], "a stale (inactive) binding must not contribute an IP"


def test_build_rules_emits_one_rule_per_bump_domain(conn):
    _insert_domain(conn, "crunchyroll\\.com")
    _insert_domain(conn, "some-other-bump-site\\.com")
    _insert_domain(conn, "a-splice-domain\\.com", mode="splice")  # must be ignored entirely
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False)

    rules = adguard_sync.build_rules(conn)

    assert len(rules) == 2
    assert any("crunchyroll" in r for r in rules)
    assert any("some-other-bump-site" in r for r in rules)


def test_build_rules_denies_a_bump_eligible_device_from_an_unassigned_non_global_domain(conn):
    """The core 2026-08-31 rework: a bump-eligible device used to get a
    free DNS pass to ANY bump-mode domain -- now AdGuard also checks
    whether this SPECIFIC domain is actually assigned to it, same as
    Squid's own authz_helper.decide() does after decryption."""
    domain_id = _insert_domain(conn, "example\\.com", is_global=False)
    _insert_device_with_binding(
        conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=True, is_authenticated=True,
    )

    rules = adguard_sync.build_rules(conn)

    assert len(rules) == 1
    assert "192.168.1.10" in rules[0]


def test_build_rules_allows_a_bump_eligible_device_once_the_domain_is_assigned(conn):
    domain_id = _insert_domain(conn, "example\\.com", is_global=False)
    device_id = _insert_device_with_binding(
        conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=True, is_authenticated=True,
    )
    conn.execute("INSERT INTO device_domains (device_id, domain_id) VALUES (?, ?)", (device_id, domain_id))
    conn.commit()

    assert adguard_sync.build_rules(conn) == []


def test_build_rules_still_denies_a_non_bump_eligible_device_even_on_a_global_domain(conn):
    """is_global on a bump-mode domain means "assigned to everyone" --
    it must never also mean "skip the bump-eligibility gate"."""
    _insert_domain(conn, "example\\.com", is_global=True)
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False)

    rules = adguard_sync.build_rules(conn)

    assert len(rules) == 1
    assert "192.168.1.10" in rules[0]


def test_build_rules_excludes_an_ignored_device(conn):
    """2026-08-31, project owner's explicit direction: "AdGuard should
    apply a baseline of protection... unless the device/user/group is
    set to bypass/ignore." An ignored device must never appear in a deny
    rule, even one that would otherwise clearly be denied."""
    _insert_domain(conn, "example\\.com", is_global=False)
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False, ignored=True)

    assert adguard_sync.build_rules(conn) == []


# ============================================================
# build_splice_deny_rules (added 2026-08-31, see its own docstring / GH #9)
# ============================================================

def test_build_splice_deny_rules_denies_a_device_with_no_assignment(conn):
    _insert_domain(conn, "example\\.com", mode="splice", is_global=False)
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10")

    rules = adguard_sync.build_splice_deny_rules(conn)

    assert len(rules) == 1
    assert "192.168.1.10" in rules[0]


def test_build_splice_deny_rules_excludes_a_user_assigned_device(conn):
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) "
        "VALUES ('kid1', 'kid1', 'x', datetime('now'))"
    )
    conn.commit()
    user_id = conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()["id"]
    domain_id = _insert_domain(conn, "example\\.com", mode="splice", is_global=False)
    conn.execute("INSERT INTO user_domains (user_id, domain_id) VALUES (?, ?)", (user_id, domain_id))
    conn.commit()
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", user_id=user_id)

    assert adguard_sync.build_splice_deny_rules(conn) == []


def test_build_splice_deny_rules_excludes_a_group_assigned_device(conn):
    """The other half of the same fix common/matching.py's
    device_domain_reason() shipped for Squid -- AdGuard now respects a
    group_domains grant too, not just user_domains."""
    group_id = _insert_group(conn, "TVs")
    domain_id = _insert_domain(conn, "example\\.com", mode="splice", is_global=False)
    conn.execute("INSERT INTO group_domains (group_id, domain_id) VALUES (?, ?)", (group_id, domain_id))
    conn.commit()
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", group_id=group_id)

    assert adguard_sync.build_splice_deny_rules(conn) == []


def test_build_splice_deny_rules_ignores_global_domains(conn):
    _insert_domain(conn, "example\\.com", mode="splice", is_global=True)
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10")
    assert adguard_sync.build_splice_deny_rules(conn) == []


def test_build_splice_deny_rules_ignores_bump_mode_domains(conn):
    """Bump-mode domains are build_rules()'s own job -- this function only
    ever touches splice-mode, is_global=0 domains."""
    _insert_domain(conn, "crunchyroll\\.com", mode="bump", is_global=False)
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10")
    assert adguard_sync.build_splice_deny_rules(conn) == []


def test_build_splice_deny_rules_empty_when_no_device_has_an_ip(conn):
    _insert_domain(conn, "example\\.com", mode="splice", is_global=False)
    assert adguard_sync.build_splice_deny_rules(conn) == []


def test_build_splice_deny_rules_excludes_an_ignored_device(conn):
    """Same exclusion as build_rules() -- see its own test and
    _build_domain_deny_rules()'s docstring."""
    _insert_domain(conn, "example\\.com", mode="splice", is_global=False)
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", ignored=True)

    assert adguard_sync.build_splice_deny_rules(conn) == []


def test_sync_once_pushes_both_bump_and_splice_rule_sets(conn, monkeypatch):
    _insert_domain(conn, "crunchyroll\\.com", mode="bump")
    _insert_domain(conn, "example\\.com", mode="splice", is_global=False)
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False)

    monkeypatch.setattr(adguard_sync.adguard_client, "get_custom_rules", lambda *a, **k: [])
    pushed = {}
    monkeypatch.setattr(
        adguard_sync.adguard_client, "set_custom_rules",
        lambda base_url, u, p, rules: pushed.setdefault("rules", rules),
    )

    count = adguard_sync.sync_once(conn, "http://127.0.0.1:3000", "admin", "x")

    assert count == 2  # one bump hard-deny + one splice deny, same device denied both ways
    managed = pushed["rules"][1:-1]  # strip the begin/end markers
    assert any("crunchyroll" in r for r in managed)
    assert any("example" in r for r in managed)


# ============================================================
# _strip_managed_block
# ============================================================

def test_strip_managed_block_removes_only_the_bracketed_lines():
    rules = [
        "! admin's own rule",
        adguard_sync._MARKER_BEGIN,
        "/(?i)(?:^|\\.)(?:crunchyroll\\.com)$/$client=192.168.1.10",
        adguard_sync._MARKER_END,
        "||another-admin-rule.com^",
    ]
    assert adguard_sync._strip_managed_block(rules) == [
        "! admin's own rule",
        "||another-admin-rule.com^",
    ]


def test_strip_managed_block_is_a_noop_when_no_marker_present():
    rules = ["||some-rule.com^"]
    assert adguard_sync._strip_managed_block(rules) == rules


def test_strip_managed_block_handles_a_dangling_begin_marker():
    """An interrupted previous sync (or a hand-edit) could leave a begin
    marker with no matching end -- drop from there to the end of the
    list rather than leave a permanently-dangling block."""
    rules = ["||kept.com^", adguard_sync._MARKER_BEGIN, "/some-rule/$client=1.2.3.4"]
    assert adguard_sync._strip_managed_block(rules) == ["||kept.com^"]


# ============================================================
# sync_once
# ============================================================

def test_sync_once_preserves_admin_rules_and_replaces_the_managed_block(conn, monkeypatch):
    _insert_domain(conn, "crunchyroll\\.com")
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False)

    existing = [
        "! an admin's own rule",
        adguard_sync._MARKER_BEGIN,
        "/some-stale-rule/$client=10.0.0.1",
        adguard_sync._MARKER_END,
    ]
    monkeypatch.setattr(adguard_sync.adguard_client, "get_custom_rules", lambda *a, **k: list(existing))

    pushed = {}
    monkeypatch.setattr(
        adguard_sync.adguard_client, "set_custom_rules",
        lambda base_url, u, p, rules: pushed.setdefault("rules", rules),
    )

    count = adguard_sync.sync_once(conn, "http://127.0.0.1:3000", "admin", "x")

    assert count == 1
    assert pushed["rules"][0] == "! an admin's own rule"
    assert pushed["rules"][1] == adguard_sync._MARKER_BEGIN
    assert "192.168.1.10" in pushed["rules"][2]
    assert pushed["rules"][-1] == adguard_sync._MARKER_END


def test_sync_once_with_nothing_to_deny_still_clears_a_stale_managed_block(conn, monkeypatch):
    """No bump domains configured (or no non-bump device known yet) --
    build_rules() returns []. A previous cycle's now-stale managed block
    must still be cleared, not left in place forever."""
    existing = ["! kept", adguard_sync._MARKER_BEGIN, "/stale/$client=1.2.3.4", adguard_sync._MARKER_END]
    monkeypatch.setattr(adguard_sync.adguard_client, "get_custom_rules", lambda *a, **k: list(existing))

    pushed = {}
    monkeypatch.setattr(
        adguard_sync.adguard_client, "set_custom_rules",
        lambda base_url, u, p, rules: pushed.setdefault("rules", rules),
    )

    count = adguard_sync.sync_once(conn, "http://127.0.0.1:3000", "admin", "x")

    assert count == 0
    assert pushed["rules"] == ["! kept"]


# ============================================================
# run_loop -- wiring sync_once() into a background PeriodicTask
# ============================================================
#
# Same reasoning/pattern as test_controller_discovery.py's run_loop
# tests: run_loop() opens its OWN connection lazily on its background
# thread, so the `conn` fixture's monkeypatched db.DB_PATH is what lets
# that internal db.get_conn() call land on the same on-disk test DB
# these tests read back from.

def test_run_loop_calls_sync_repeatedly(conn, monkeypatch):
    _insert_domain(conn, "crunchyroll\\.com")
    _insert_device_with_binding(conn, "aa:bb:cc:dd:ee:01", "192.168.1.10", bump_enabled=False)

    calls = []
    monkeypatch.setattr(adguard_sync.adguard_client, "get_custom_rules", lambda *a, **k: [])
    monkeypatch.setattr(
        adguard_sync.adguard_client, "set_custom_rules",
        lambda base_url, u, p, rules: calls.append(rules),
    )

    task = adguard_sync.run_loop(interval=0.02, base_url="http://x", username="a", password="b")
    try:
        time.sleep(0.15)
    finally:
        task.stop()

    assert len(calls) >= 2, "expected sync_once to run repeatedly on the interval"
    assert any("192.168.1.10" in r for r in calls[0])


def test_run_loop_stops_promptly(conn, monkeypatch):
    monkeypatch.setattr(adguard_sync.adguard_client, "get_custom_rules", lambda *a, **k: [])
    monkeypatch.setattr(adguard_sync.adguard_client, "set_custom_rules", lambda *a, **k: None)

    task = adguard_sync.run_loop(interval=0.05, base_url="http://x", username="a", password="b")
    time.sleep(0.02)
    started_stop = time.monotonic()
    task.stop()
    elapsed = time.monotonic() - started_stop
    assert elapsed < 0.5, f"stop() took {elapsed:.3f}s, expected it to return promptly"


def test_run_loop_reports_sync_errors_via_on_error_without_dying(conn, monkeypatch):
    def _boom(*a, **k):
        raise adguard_sync.adguard_client.AdGuardError("adguard unreachable")

    monkeypatch.setattr(adguard_sync.adguard_client, "get_custom_rules", _boom)

    errors = []
    lock = threading.Lock()

    def on_error(exc):
        with lock:
            errors.append(exc)

    task = adguard_sync.run_loop(
        interval=0.02, base_url="http://x", username="a", password="b", on_error=on_error
    )
    try:
        time.sleep(0.1)
    finally:
        task.stop()

    with lock:
        got = len(errors)
    assert got >= 2, f"expected the loop to keep retrying (and reporting) after failures, got {got}"
