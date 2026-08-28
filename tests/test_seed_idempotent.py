"""defaults/seed_defaults.py: idempotent first-run seeding."""
from __future__ import annotations

import seed_defaults


def _counts(conn):
    return {
        "domains": conn.execute("SELECT COUNT(*) c FROM domains").fetchone()["c"],
        "domain_paths": conn.execute("SELECT COUNT(*) c FROM domain_paths").fetchone()["c"],
    }


def test_seed_is_idempotent_on_row_counts(conn):
    seed_defaults.seed(conn)
    conn.commit()
    first = _counts(conn)

    seed_defaults.seed(conn)
    conn.commit()
    second = _counts(conn)

    assert first == second
    assert first["domains"] > 0
    assert first["domain_paths"] == len(seed_defaults.CRUNCHYROLL_PATHS)


def test_seed_twice_leaves_admin_edits_untouched(conn):
    seed_defaults.seed(conn)
    conn.commit()
    conn.execute("UPDATE domains SET mode = 'bump' WHERE pattern = ?", (r"google\.com",))
    conn.commit()

    seed_defaults.seed(conn)
    conn.commit()

    row = conn.execute("SELECT mode FROM domains WHERE pattern = ?", (r"google\.com",)).fetchone()
    assert row["mode"] == "bump"  # re-seeding must not clobber an admin's change


def test_crunchyrollcdn_is_trusted_not_splice(conn):
    """S2.2: crunchyrollcdn.com must end up 'trusted', not silently dropped
    into 'splice' by being listed in both domain lists."""
    seed_defaults.seed(conn)
    conn.commit()
    row = conn.execute(
        "SELECT mode FROM domains WHERE pattern = ?", (r"crunchyrollcdn\.com",)
    ).fetchone()
    assert row is not None
    assert row["mode"] == "trusted"


def test_crunchyroll_domain_seeded_as_global_bump(conn):
    seed_defaults.seed(conn)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM domains WHERE pattern = ?", (r"crunchyroll\.com",)
    ).fetchone()
    assert row is not None
    assert row["mode"] == "bump"
    assert row["kind"] == "crunchyroll"
    assert row["is_global"] == 1


def test_crunchyrollsvc_playback_host_is_not_seeded(conn):
    """S1.2, resolved 2026-08-28: confirmed against Crunchyroll's own live
    webpack bundle that production playback is served from
    www.crunchyroll.com/playback (already covered by the crunchyroll.com
    domain) -- crunchyrollsvc.com is dev-only in Crunchyroll's own config
    and must NOT be seeded as a separate domain."""
    seed_defaults.seed(conn)
    conn.commit()
    row = conn.execute(
        "SELECT * FROM domains WHERE pattern = ?", (r"crunchyrollsvc\.com",)
    ).fetchone()
    assert row is None


def test_crunchyroll_paths_attached_to_crunchyroll_domain_only(conn):
    seed_defaults.seed(conn)
    conn.commit()
    cr_row = conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"crunchyroll\.com",)).fetchone()
    count = conn.execute(
        "SELECT COUNT(*) c FROM domain_paths WHERE domain_id = ?", (cr_row["id"],)
    ).fetchone()["c"]
    assert count == len(seed_defaults.CRUNCHYROLL_PATHS)


def test_all_seeded_domain_patterns_are_unique(conn):
    seed_defaults.seed(conn)
    conn.commit()
    patterns = [r["pattern"] for r in conn.execute("SELECT pattern FROM domains")]
    assert len(patterns) == len(set(patterns))
