# Tier-1 test suite (no Docker required)

Pure-Python unit/integration tests covering everything in `common/`,
`proxy/`, `defaults/`, and `dashboard/` that doesn't require a running
Squid or a real container. This is "Phase 1" from
`docs/review-2026-08-28.md`, made real.

## Running

```
pip install pytest
pytest
```

No fixtures require Docker, and the `block_network` autouse fixture fails
any test that tries to make a real HTTP call (Crunchyroll's API is always
mocked). Every test gets its own throwaway SQLite file via `tmp_path`, so
tests can run in any order and in parallel (`pytest -n auto`, with
`pytest-xdist` installed).

## Layout

| File | Covers |
|---|---|
| `test_matching.py` | Domain-suffix anchoring (incl. the S2.1 bypass cases), path matching, LAN CIDR checks (IPv4/IPv6, bad input) |
| `test_cr_urls.py` | Every `RequestKind`, including the fail-closed `BLOCKED_SHAPE` cases |
| `test_auth.py` | PBKDF2 hash round-trip, and every way a stored hash can be malformed/tampered |
| `test_logging_dedupe.py` | Access-log dedupe window, keyed by `(username, domain, allowed, series_id)` |
| `test_seed_idempotent.py` | `seed_defaults.seed()` run twice is a no-op; the S2.2 (`crunchyrollcdn.com` mode) and S1.2 (`crunchyrollsvc.com` seeded) fixes |
| `test_helpers_protocol.py` | The shared Squid stdin/stdout protocol (`common/squid_helper.py`) plus the actual decision logic of all three helpers (`basic_auth_helper`, `sni_helper`, `authz_helper`), including the Crunchyroll show-approval flow |
| `test_cr_api.py` | `TokenManager` refresh/expiry, `SeriesResolver`'s retry-once-on-401, every `_read_json` error path (HTTP error, unreachable, timeout, malformed/non-dict JSON), and every `series_id_of` fallback branch |
| `test_series_resolve.py` | The object-id -> series-id cache: positive/negative TTL, cache-hit short-circuiting, and the S2.6 stale-on-error fallback (see "Bug found" below) |
| `test_dashboard.py` | Flask test client: admin auth, user/domain CRUD (incl. `add_show`/`remove_show`, `user_detail`, `domain_detail`, `update_domain`), all of Settings (including changing the admin login itself), report-approve (site and show), and the CSRF/cross-origin guard |

## Bug found while writing this suite

`common/series_resolve.py`'s `_cache_get()` used to delete an expired
*positive* cache entry unconditionally, including on the first (non-stale)
lookup pass that `resolve_series_ids()` always does before ever calling the
resolver. That meant the S2.6 "serve a stale entry if the resolver fails"
mitigation could never actually find anything to serve -- the row was
already gone by the time the fallback ran. Fixed to leave the row in place
on a non-stale miss (matching how the negative-cache branch already
behaved); a successful re-resolve overwrites it via `_cache_put`'s
`ON CONFLICT ... DO UPDATE` either way. See
`test_series_resolve.py::test_resolver_failure_with_stale_positive_cache_serves_stale`.

## What this does *not* cover

Anything requiring a real Squid process, real Docker networking, or a real
device -- see "Phase 2/3" in `docs/review-2026-08-28.md` for that (untested
`external_acl_type`/`ssl_bump` combination, `%>a` behavior under Docker
Desktop bridge networking, real Crunchyroll playback).
