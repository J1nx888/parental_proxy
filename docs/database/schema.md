# Database schema

Single shared SQLite database file used by every component: the Squid-side
Python helpers (`proxy/basic_auth_helper.py`, `proxy/sni_helper.py`,
`proxy/authz_helper.py`), the caching layer (`common/series_resolve.py`),
and the Flask dashboard (`dashboard/dashboard.py`). All of them open the
same file via `common/db.py`; there is no ORM and no ID-generation scheme
beyond SQLite's own `INTEGER PRIMARY KEY` rowid aliasing.

## Where the schema lives

**File:** `common/db.py`
**Constant:** `SCHEMA` (a single multi-statement SQL string, lines 18-97)
**Applied by:** `init_db(conn)` (line 110), which just runs
`conn.executescript(SCHEMA)`.

- **Migration style:** none. Every statement is `CREATE TABLE IF NOT EXISTS`
  / `CREATE INDEX IF NOT EXISTS`. There is no schema-version table, no
  ordered migration list, and no `ALTER TABLE` anywhere in the codebase.
  Changing a column's type/constraints today means editing the `CREATE
  TABLE` statement in place, which only takes effect for brand-new database
  files -- an existing deployed `.db` file keeps its old column definitions
  until someone manually migrates it (or the table is dropped and
  recreated). Keep this in mind before assuming an edit to `SCHEMA` retrofits
  existing installs.
- **Called from:** `init_db()` runs on every dashboard request that opens a
  connection (`dashboard.get_db()`, dashboard.py:59-62) and on every proxy
  container start (`proxy/entrypoint.sh` line 35, inline Python heredoc).
  It's cheap and idempotent, so it's safe to call unconditionally rather
  than gating it behind a "first run" check.
- **Connection setup:** `get_conn()` (db.py:100-107) opens the file at
  `DB_PATH`, sets `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`,
  `PRAGMA foreign_keys=ON`, and `row_factory = sqlite3.Row` (so query
  results are dict-like, accessed by column name -- `row["field"]` -- for
  every query in the codebase). WAL + a busy timeout are what let the proxy
  container and the dashboard container hit the same file concurrently
  without a real database server.

## Tables

### `settings`
Generic admin-editable key/value store. Everything configurable that isn't
better modeled as its own table lives here.

| Column | Type | Constraints |
|---|---|---|
| `key`   | TEXT | PRIMARY KEY |
| `value` | TEXT | NOT NULL |

Accessed only through `get_setting(conn, key, default=None)`,
`set_setting(conn, key, value)` (upsert via `ON CONFLICT(key) DO UPDATE`),
and `set_setting_if_absent(conn, key, value)` (`ON CONFLICT(key) DO
NOTHING`) in `common/db.py`. Known keys actually written/read elsewhere:

- `admin_username`, `admin_password_hash` -- dashboard HTTP Basic admin
  login, bootstrapped from `DASHBOARD_USER`/`DASHBOARD_PASSWORD` env vars on
  first run by `dashboard.bootstrap_admin()`, editable afterward from the
  Settings page (`update_admin`).
- `local_network` -- space-separated CIDR list (e.g.
  `192.168.1.0/24 10.0.0.0/24`) checked by `matching.ip_in_configured_lan()`.
  Empty string disables the LAN check entirely (access then rests solely on
  per-person proxy login), which matters under Docker bridge/Desktop
  networking where the proxy only ever sees an internal gateway IP.
- `block_page_mode` -- `"terminate"` (default) or `"redirect"`. Read by
  `sni_helper.handle_block_page()` to decide whether an unrecognized/denied
  splice-mode connection is bumped so a real HTTP deny page can be served,
  or just fails the TLS connection outright. See the enum section below.
- `secret_key` -- random hex token generated once (`secrets.token_hex(32)`)
  and used as the Flask app's `app.secret_key`.
- `cr_resolver_last_error` -- last Crunchyroll-CMS resolver failure message,
  set by `series_resolve._record_resolver_error()` and cleared by
  `_clear_resolver_error()` on the next success. Surfaced as a banner on the
  dashboard's Report page (`dashboard.report()`).

### `users`
One row per proxy login (one per kid/person), independent of the dashboard
admin login (which lives in `settings`, not here).

| Column | Type | Constraints |
|---|---|---|
| `id`            | INTEGER | PRIMARY KEY |
| `username`      | TEXT | UNIQUE NOT NULL |
| `display_name`  | TEXT | NOT NULL |
| `password_hash` | TEXT | NOT NULL |
| `created_at`    | TEXT | NOT NULL (ISO-8601 UTC, see `db.now_iso()`) |

`username` is what's configured in a device's proxy auth settings and is
checked by `proxy/basic_auth_helper.py:check()` against `password_hash`
(via `common/auth.py`'s `verify_password`). Usernames are restricted by the
dashboard's `add_user()` to `[A-Za-z0-9_.-]+`. Deleting a user
(`dashboard.delete_user()`) cascades to `user_domains` and `user_shows` via
`ON DELETE CASCADE` (enforced because `PRAGMA foreign_keys=ON` is set on
every connection).

### `domains`
Every domain the proxy knows about and how to treat it. This is the core
policy table -- `matching.find_domain()` scans it in `id` order (first
regex match wins) for essentially every proxy decision.

| Column | Type | Constraints |
|---|---|---|
| `id`         | INTEGER | PRIMARY KEY |
| `pattern`    | TEXT | UNIQUE NOT NULL -- a regex, not a literal hostname |
| `mode`       | TEXT | NOT NULL, `CHECK (mode IN ('splice','bump','trusted'))` |
| `kind`       | TEXT | NOT NULL DEFAULT `'generic'`, `CHECK (kind IN ('generic','crunchyroll'))` |
| `is_global`  | INTEGER | NOT NULL DEFAULT 0 (boolean: 1/0) |
| `note`       | TEXT | nullable, admin-facing free text |
| `created_at` | TEXT | NOT NULL (ISO-8601 UTC) |

**`pattern`** is compiled by `matching._domain_regex()` as
`(?:^|\.)(?:<pattern>)\Z` (case-insensitive), i.e. an anchored domain-suffix
match -- `crunchyroll\.com` matches `crunchyroll.com` and any subdomain, but
not `evilcrunchyroll.com` or `crunchyroll.com.attacker.example`. An invalid
regex is skipped rather than raising (results in `find_domain` returning
`None` for that row, effectively disabling it). Patterns are admin-supplied
free-form regex, validated at write time only by `re.compile()` succeeding
and a 200-character cap (`dashboard.add_domain()` / `add_path()`).

**`mode`** (enum, exhaustive per the CHECK constraint):
- `splice` -- host-only check via SNI, connection is never decrypted.
  Evaluated by `sni_helper.handle_splice()`. This is the *only* stage that
  ever observes splice-mode traffic, so it's the only place that logs it.
- `bump` -- fully decrypted (`sni_helper.handle_bump()` tells Squid to
  bump); path/show-level rules then apply at the HTTP layer via
  `authz_helper.decide()`.
- `trusted` -- always spliced, never checked or logged at all (deliberately
  invisible -- see `sni_helper.py` module docstring). Used for large binary
  CDN traffic (Crunchyroll's raw video CDN hosts) where the meaningful
  authorization decision already happened upstream, at the bump-mode
  manifest/playback-token request.

**`kind`** (enum): `generic` (default) or `crunchyroll`. A `crunchyroll`
domain gets an extra show-level resolution layer on top of the normal
domain/path checks (`authz_helper._decide_crunchyroll()`); only one such
row is ever seeded (see Seed data below), and the dashboard explicitly
refuses to let it be deleted (`delete_domain()`).

**`is_global`**: `1` means every user gets this domain automatically (no
`user_domains` row needed) -- used for shared infrastructure the whole
household depends on (Google, CDNs, cookie-consent scripts, etc.) as well
as the trusted CDN hosts and the Crunchyroll domain itself. `0` means each
user needs an explicit `user_domains` row.

### `user_domains`
Per-user domain assignment (many-to-many `users` <-> `domains`), used only
for domains where `is_global = 0`.

| Column | Type | Constraints |
|---|---|---|
| `id`        | INTEGER | PRIMARY KEY |
| `user_id`   | INTEGER | NOT NULL, `REFERENCES users(id) ON DELETE CASCADE` |
| `domain_id` | INTEGER | NOT NULL, `REFERENCES domains(id) ON DELETE CASCADE` |
| | | `UNIQUE(user_id, domain_id)` |

Checked by `matching.user_has_domain(conn, user_id, domain_id)` (plain
existence check). Rows are added/removed via
`dashboard.toggle_user_domain()` (`INSERT OR IGNORE` / `DELETE`) and by the
paste-a-URL shortcut `add_domain_from_url()`. Deleting either the user or
the domain removes the assignment automatically (cascade).

### `domain_paths`
Allowed URL-path patterns for a `bump`-mode domain. **Global per domain,
not per-user** -- this is defense-in-depth against unrecognized request
shapes, the spiritual successor to v1's flat `allowed_paths.txt`, and
intentionally does not vary per kid.

| Column | Type | Constraints |
|---|---|---|
| `id`        | INTEGER | PRIMARY KEY |
| `domain_id` | INTEGER | NOT NULL, `REFERENCES domains(id) ON DELETE CASCADE` |
| `pattern`   | TEXT | NOT NULL |
| | | `UNIQUE(domain_id, pattern)` |

`pattern` is a regex checked with `.search()` (not anchored automatically --
admins are expected to write `^/...` themselves; see
`dashboard.path_to_pattern()`, which prefixes `^` and `re.escape()`s a
literal path when deriving a suggested pattern from a real blocked
request). Semantics, from `matching.path_allowed()` and its caller
`authz_helper._has_any_path_rules()`: **a domain with zero `domain_paths`
rows allows every path** once the domain check itself passes -- admins only
need to curate paths for domains where restricting *which* pages are
reachable actually matters (this is why the Crunchyroll domain is
pre-seeded with an extensive path list but most domains have none).

### `user_shows`
Per-user approved Crunchyroll series (independent of `domain_paths` --
show-level authorization for the `crunchyroll` `kind` domain).

| Column | Type | Constraints |
|---|---|---|
| `id`          | INTEGER | PRIMARY KEY |
| `user_id`     | INTEGER | NOT NULL, `REFERENCES users(id) ON DELETE CASCADE` |
| `series_id`   | TEXT | NOT NULL (Crunchyroll series id, stored upper-cased) |
| `series_name` | TEXT | NOT NULL (display name, editable, refreshed on re-approve) |
| | | `UNIQUE(user_id, series_id)` |

Checked by `matching.user_has_show(conn, user_id, series_id)`, which
upper-cases `series_id` before comparing (series IDs are treated
case-insensitively everywhere, normalized to upper-case at every write:
`dashboard.add_show()`, `dashboard.approve_from_report()`,
`series_resolve._cache_put()`). Rows are inserted with `INSERT ... ON
CONFLICT(user_id, series_id) DO UPDATE SET series_name = excluded.series_name`
so re-approving an already-approved show just refreshes its display name.

### `series_cache`
Crunchyroll CMS object-id -> parent series-id resolution cache (an object
id from a watch/playback URL isn't itself a series id; resolving it costs a
CMS API call, so the mapping is cached since it never changes once known).

| Column | Type | Constraints |
|---|---|---|
| `object_id`  | TEXT | PRIMARY KEY (upper-cased) |
| `series_id`  | TEXT | nullable -- see sentinel note below |
| `expires_at` | REAL | NOT NULL (Unix timestamp, `time.time()`-based) |

Managed entirely by `common/series_resolve.py`:
- **Positive entries** (`series_id` = real series id) live for
  `POSITIVE_TTL_SECONDS` = 30 days, and are deliberately still served
  *past* expiry (`allow_stale=True`) if the live Crunchyroll API is
  unreachable at the time -- a parent series never changes, so a stale
  positive hit is still correct, and this keeps already-approved playback
  working through a resolver/network outage instead of breaking it.
- **Negative entries** use the string sentinel `_MISSING =
  "__missing__"` as the stored `series_id` value (so a negative hit is
  `series_id IS NULL` in code, i.e. resolvable-to-nothing, without needing
  a separate boolean "found" column) and expire after
  `NEGATIVE_TTL_SECONDS` = 1 hour; an expired negative entry is always
  re-checked rather than served stale, since blocking a show forever on a
  transient lookup failure would be a worse failure mode than a delayed
  block.
- Writes go through `_cache_put()`'s
  `INSERT ... ON CONFLICT(object_id) DO UPDATE SET series_id = excluded.series_id,
  expires_at = excluded.expires_at` (upsert), so re-resolving the same
  object id just overwrites the row.

### `access_log`
Append-only audit trail of every access decision the proxy makes (subject
to the dedupe window described below). Backs the dashboard's Report page
and its one-click "approve" action.

| Column        | Type    | Constraints |
|---|---|---|
| `id`          | INTEGER | PRIMARY KEY |
| `ts`          | TEXT    | NOT NULL (ISO-8601 UTC, `db.now_iso()`) |
| `user_id`     | INTEGER | nullable -- NULL when the request has no resolvable user (e.g. `not_authenticated`, or a login string that didn't match any `users` row) |
| `username`    | TEXT    | NOT NULL -- the raw login string, even when `user_id` is NULL (e.g. `"(unauthenticated)"` for the never-logged-in case, see `sni_helper._log_denial()`) |
| `domain`      | TEXT    | NOT NULL -- the hostname actually observed (SNI or `%DST`), not the `domains.pattern` regex that matched it |
| `path`        | TEXT    | nullable -- always NULL for SNI-layer (splice) entries, since nothing is decrypted at that layer to reveal a path |
| `series_id`   | TEXT    | nullable -- set only for Crunchyroll show-level decisions |
| `series_name` | TEXT    | nullable -- set only alongside `series_id` when a name is already known (report-driven approvals resolve/attach a name; live proxy-time denials generally don't have one yet) |
| `allowed`     | INTEGER | NOT NULL (boolean: 1/0) |
| `reason`      | TEXT    | free-text enum, see below -- not constrained by a CHECK, just convention |

**Indexes:**
- `idx_access_log_ts` on `access_log(ts DESC)` -- supports the Report
  page's `ORDER BY id DESC LIMIT 200` browsing pattern (recency).
- `idx_access_log_dedupe` on
  `access_log(username, domain, allowed, series_id, ts DESC)` -- supports
  the dedupe lookup in `common/logging_util.py` (see below).

All writes to this table go through the single entry point
`logging_util.log_access()` (`common/logging_util.py`) -- there is no other
`INSERT INTO access_log` in the codebase. Reads happen from
`dashboard.report()` (filtered listing) and `dashboard.approve_from_report()`
(single-row lookup by `id` to drive the one-click approve action).

#### Dedupe logic (`common/logging_util.py: log_access()`)
The same `(username, domain, allowed, series_id, path)` combination is only
written once per `DEDUPE_WINDOW_SECONDS` (5 minutes), so one browsing
session doesn't produce dozens of near-identical rows from repeated TLS
connections, page assets, or polling requests. Two notable SQL techniques
worth knowing when touching this table:

- **Null-safe comparison via `IS`:** the dedupe `SELECT` uses
  `series_id IS ?` and a path comparison also written with `IS ?` (not
  `= ?`), because SQLite's `=` never matches when either side is `NULL`
  while `IS` treats two `NULL`s as equal. This matters because `series_id`
  and `path` are frequently `NULL` (SNI-layer entries, non-Crunchyroll
  domains) and two such rows still need to collapse into one dedupe bucket.
- **In-SQL path normalization:** `path` is compared with its query string
  stripped on both sides -- computed in Python for the incoming value
  (`path.split("?", 1)[0]`) and in SQL for the already-stored value via
  `CASE WHEN instr(path, '?') > 0 THEN substr(path, 1, instr(path, '?') - 1)
  ELSE path END`, so `?cachebust=123` variants of the same page collapse
  into one dedupe entry while genuinely different pages on the same domain
  each still get their own row.

## Relationships (ER summary)

```
users (1) ──< user_domains >── (1) domains ──< domain_paths
  │                                  │
  └──< user_shows                    └── (self-contained: mode/kind/is_global)

access_log: loosely references users.id via user_id (nullable, no FK
constraint -- a log row must survive its user being deleted, so history
isn't lost) and free-text domain/series_id (no FK to domains/user_shows at
all -- a denied request may reference a domain or series that has no
`domains`/`user_shows` row yet, since a large part of this table's purpose
is recording activity for things that AREN'T configured/approved yet).

series_cache: standalone, keyed by Crunchyroll object_id, not related to
any other table by a real key (series_id values it stores are compared as
plain strings against user_shows.series_id, no FK).

settings: standalone key/value, unrelated to any other table.
```

Only `user_domains`, `domain_paths`, and `user_shows` have real foreign-key
constraints (all `ON DELETE CASCADE`, all enforced live because
`PRAGMA foreign_keys=ON` is set on every connection in `get_conn()`).
`access_log.user_id` is intentionally just a plain nullable INTEGER with no
`REFERENCES` clause -- deleting a user must not delete their history, and a
row logged for an unrecognized login string never had a real `user_id` to
reference in the first place.

## Enum-like columns, exhaustively

### `domains.mode`
Enforced by a `CHECK` constraint -- exactly these three values:
`splice`, `bump`, `trusted` (see the `domains` table section above for
semantics).

### `domains.kind`
Enforced by a `CHECK` constraint -- exactly these two values:
`generic`, `crunchyroll`.

### `access_log.reason`
**Not** constrained by the schema (plain `TEXT`) -- values are just a
convention followed by every write site. Enumerated by grepping every
`reason=` assignment in `proxy/sni_helper.py` and `proxy/authz_helper.py`:

From `proxy/sni_helper.py` (SNI/splice layer, `path` always NULL):
- `not_authenticated` -- `handle_splice()`: no login present at all (`%LOGIN` was `-`/empty).
- `outside_lan` -- `handle_splice()`: client IP failed `ip_in_configured_lan()`.
- `global_domain` -- `handle_splice()`: allowed because `domains.is_global = 1`.
- `user_domain` -- `handle_splice()`: allowed because of an explicit `user_domains` row.
- `unknown_domain` -- `handle_block_page()`: connection reached the catch-all rule and genuinely has no matching `domains` row at all (only logged when `block_page_mode != 'redirect'`, since redirect mode instead falls through to `authz_helper.decide()` which logs a richer, path-bearing version of the same case).

From `proxy/authz_helper.py` (HTTP layer, bump-mode domains, `path` always populated):
- `outside_lan` -- `decide()`: client IP failed the LAN check (mirrors the SNI-layer reason of the same name).
- `unknown_domain` -- `decide()`: no `domains` row matches this hostname at all.
- `not_bump_mode` -- `decide()`: a `domains` row exists but its `mode` isn't `bump` (reached via the ssl_bump `block_page` redirect path for a denied splice-mode domain).
- `domain_not_assigned` -- `decide()`: domain exists and is `bump` mode, but is neither global nor assigned to this user.
- `path_not_allowed` -- `decide()` (generic bump domain) and `_decide_crunchyroll()` (Crunchyroll `OTHER`-shape fallback): domain permitted, but the request path matched none of its `domain_paths` patterns (only checked when the domain has at least one path rule at all).
- `global_domain` -- `decide()`: generic bump-mode domain allowed because it's global.
- `user_domain` -- `decide()`: generic bump-mode domain allowed via an explicit `user_domains` row.
- `blocked_shape` -- `_decide_crunchyroll()`: `cr_urls.classify()` recognized the URL as a shape that's always denied.
- `show_approved` / `show_not_approved` -- `_decide_crunchyroll()` (both the direct `SERIES_PAGE` case and the resolved `WATCH_PAGE`/`PLAYBACK` case): whether `matching.user_has_show()` found this `series_id` in the user's `user_shows`. Logged once per series id when a request references more than one (e.g. a playback request naming multiple objects).
- `resolution_failed` -- `_decide_crunchyroll()`: `series_resolve.resolve_series_ids()` returned `None` (CMS API unreachable and no usable stale-positive cache entry existed for every requested object id) -- fails closed.

`CMS_OBJECTS`-kind Crunchyroll requests (pure metadata) are never logged at
all -- they're allowed unconditionally and return before reaching any
`log_access()` call (matches v1 behavior, see `_decide_crunchyroll()`).

## Seed data (`defaults/seed_defaults.py`)

Run once per container start by `proxy/entrypoint.sh` (also safe to run
manually/repeatedly -- see "Reset / run locally" below). Purely additive
and non-destructive: every insert is `INSERT OR IGNORE`, so re-running
against an already-configured database changes nothing, and anything an
admin has since edited or removed via the dashboard stays exactly as they
left it.

1. **`GLOBAL_SPLICE_DOMAINS`** (23 entries) -- infrastructure Crunchyroll's
   site depends on, inserted as `mode='splice'`, `kind='generic'`,
   `is_global=1`. Carried over from v1's flat `allowed_sites.txt`. Includes
   Google/gstatic/googleapis/googleusercontent, `ctfassets.net` (Crunchyroll
   CMS assets), `vimeo.com`, analytics/telemetry (`segment.com`,
   `datadoghq.com`, `googletagmanager.com`), notification/SSO providers
   (`braze.com`, `auth0.com`, `firebaseapp.com`), geolocation services
   (`ipify.org`, `ipapi.co`, `iplocate.io`, `ipinfo.io`), the video player
   stack (`bitmovin.com`, `litix.io`), cookie-consent vendors
   (`cookielaw.org`, `ketchcdn.com`, `ketchjs.com`, `onetrust.com`), a CDN
   (`akamaized.net`), and a script CDN (`jsdelivr.net`).
2. **`TRUSTED_DOMAINS`** (2 entries) -- `gccrunchyroll.com` and
   `crunchyrollcdn.com`, inserted as `mode='trusted'`, `is_global=1`.
   Crunchyroll's raw video CDN hosts: large binary traffic with nothing
   meaningful to authorize per-request, since the real per-show decision
   already happened at the bump-mode manifest/playback-token request.
3. **The Crunchyroll domain itself** -- a single row,
   `pattern='crunchyroll\.com'`, `mode='bump'`, `kind='crunchyroll'`,
   `is_global=1`, note `"Crunchyroll -- shows approved per-user"`. This is
   the one domain the dashboard refuses to let an admin delete
   (`dashboard.delete_domain()`). Note the seed script's comment recording
   that a separate `crunchyrollsvc.com` playback-service domain was
   deliberately *not* added (confirmed against Crunchyroll's live webpack
   bundle 2026-08-28 that production playback is served from
   `www.crunchyroll.com/playback`, not the dev-only `cr-play-service.*
   .crunchyrollsvc.com` host).
4. **`CRUNCHYROLL_PATHS`** (~25 regex patterns) -- inserted into
   `domain_paths` for the Crunchyroll domain row above (looked up by
   `pattern = 'crunchyroll\.com'` immediately after inserting it). Defense
   in depth for request shapes `cr_urls.classify()` doesn't specifically
   recognize -- login/auth/API endpoints, static assets (`css/js/images`),
   discover/browse pages, versioned API paths (`/subs/v\d+/`,
   `/playback/v\d+/`, `/accounts/v\d+/`, `/content/v\d+/`, etc.), i18n,
   skip-events, playhead tracking, and personalization/recommendation rows
   (`/personalization/v\d+/`, added for GH #4's "Top 10" home-page rows).
   Carried over from v1's flat `allowed_paths.txt`.

Run directly: `python3 defaults/seed_defaults.py` (imports `db` from the
same directory via a `sys.path.insert` at the top of the file -- run it
from a context where `common/` is importable as `db`, e.g. from inside the
proxy container's `/opt/parental-proxy/`, or with that directory added to
`PYTHONPATH`/`sys.path` manually).

## Running / resetting the database locally

- **Path:** controlled entirely by the `PP_DB_PATH` environment variable,
  read once at import time into the module-level `db.DB_PATH` (default
  `/config/parental_proxy.db`, i.e. the shared Docker volume `pp_config`
  mounted into both the `proxy` and `dashboard` containers per
  `docker-compose.yml`). There is no CLI flag -- only the env var.
- **Sidecar files:** because `journal_mode=WAL` is on, expect
  `parental_proxy.db-wal` and `parental_proxy.db-shm` alongside the main
  file; a full reset means removing all three (or the whole volume).
- **No dedicated reset script.** To start clean:
  - **Docker:** `docker compose down -v` removes the `pp_config` volume
    entirely (deletes the DB, the WAL/SHM sidecars, and the generated CA
    certificate together), then `docker compose up` recreates everything
    and re-runs `proxy/entrypoint.sh`'s seeding on next start.
  - **Locally / outside Docker:** delete the file at whatever `PP_DB_PATH`
    points to (plus its `-wal`/`-shm` siblings if present), then run
    `python3 defaults/seed_defaults.py` (which itself calls
    `db.init_db()` before seeding) to get a freshly-schema'd, freshly-seeded
    database again.
  - **Tests:** `tests/conftest.py`'s `conn` fixture gives every test a
    throwaway SQLite file under `tmp_path` with the schema applied and
    *no* seed data (`db.init_db()` only, `seed()` is not called) --
    monkeypatches `db.DB_PATH` directly rather than touching the env var,
    since `db.py` reads `PP_DB_PATH` once at import time. A safe fallback
    path (`%TEMP%/parental_proxy_pytest_default.db` equivalent) is set via
    `os.environ.setdefault("PP_DB_PATH", ...)` at collection time so a
    stray early import of `db` never touches a real `/config` path.
- **Inspecting the live file directly:** it's a normal SQLite database --
  `sqlite3 /path/to/parental_proxy.db` (or any SQLite browser) works, but
  do so read-only or briefly while both containers are stopped/idle if
  writing, since the WAL mode assumes cooperating connections rather than
  an external tool holding a long write transaction.
