# Architecture Overview

Reference for navigating and modifying this codebase. Written against the
tree as of 2026-08-28 (see `docs/review-2026-08-28.md` for the live-testing
bugfix history referenced throughout this doc).

This is a parental-control **forward proxy**: a single Squid instance with
SSL-Bump enabled, sitting between LAN clients and the internet, making
per-person allow/deny decisions by calling out to small Python scripts that
read/write one shared SQLite database. A Flask dashboard is the only way an
admin edits that database's contents.

---

## 1. Components and exact file locations

```
common/                      shared Python modules, imported by both containers
  db.py                        SQLite schema (SCHEMA string) + get_conn()/init_db()
  auth.py                      PBKDF2-SHA256 password hashing (hash_password/verify_password)
  matching.py                  domain/path regex matching, LAN CIDR check
  logging_util.py              log_access() -- deduped access-log writer
  squid_helper.py              shared stdin/stdout protocol loop (run())
  series_resolve.py            Crunchyroll object-id -> series-id cache (resolve_series_ids())
  cr_api.py                    Crunchyroll CMS API client (TokenManager, SeriesResolver)
  cr_urls.py                   Crunchyroll URL classifier (classify(), RequestKind enum)

proxy/                        Squid container
  Dockerfile                    debian:bookworm-slim + squid-openssl
  entrypoint.sh                 first-run seeding, CA cert generation, squid.conf render
  squid.conf.template           the actual Squid config (http_port, acls, http_access, ssl_bump)
  basic_auth_helper.py          auth_param basic program -- per-person login (check())
  sni_helper.py                 external_acl_type for ssl_bump step2 (handle_bump/handle_trusted/
                                 handle_splice/handle_block_page)
  authz_helper.py               external_acl_type for the HTTP-layer decision (decide())

dashboard/                    Flask container
  dashboard.py                  all routes: users, domains, paths, shows, report, settings, /ca-cert
  Dockerfile                    python:3.12-slim + waitress
  requirements.txt              flask>=3.0, waitress>=3.0

defaults/
  seed_defaults.py              idempotent first-run seed data (seed())

docker-compose.yml             two services (proxy, dashboard) sharing the pp_config volume
```

Both container images `COPY common/*.py` into their own image root (proxy's
`/opt/parental-proxy/`, dashboard's `/app/`) and add that directory to
`sys.path`. There is no shared package install step -- it's a flat file copy,
so a change to any `common/*.py` file requires rebuilding **both** images to
take effect everywhere.

---

## 2. Tech stack

- **Python 3** -- proxy container uses whatever `python3` Debian bookworm-slim
  ships (stdlib-only: `auth.py` is deliberately stdlib so the proxy container
  never needs `pip`); dashboard container is pinned to `python:3.12-slim`.
- **Squid, specifically `squid-openssl`** -- see `proxy/Dockerfile` comment:
  Debian's plain `squid` package is built `--with-gnutls` and does not support
  SSL-Bump at all (`squid -k parse` fails on the `ssl-bump` http_port option).
  `squid-openssl` is the same version built `--with-openssl`, which
  `ssl_bump`/`sslcrtd_program` require. This is a hard requirement, not an
  optimization -- installing plain `squid` breaks the entire project.
- **Flask** (dashboard web framework) served by **waitress** (`dashboard.py`
  `main()`), not Flask's dev server.
- **SQLite** (stdlib `sqlite3`), WAL mode, as the only datastore -- no
  Postgres/Redis/etc.
- **Docker / docker-compose** -- two services, one named volume.

---

## 3. Component diagram (text form)

```
                         LAN client (kid's device)
                                   |
                    TLS ClientHello / CONNECT + per-person
                    Basic-Auth login (device proxy settings)
                                   v
                 +--------------------------------------+
                 |         proxy container (Squid)       |
                 |  squid.conf (rendered from            |
                 |  squid.conf.template by entrypoint.sh)|
                 |                                        |
                 |  auth_param basic -> basic_auth_helper.py
                 |  ssl_bump step1/step2 -> sni_helper.py |
                 |  http_access (post-bump) -> authz_helper.py
                 +--------------------+-------------------+
                                      |
                     reads/writes    (import db, matching,
                                      logging_util, series_resolve,
                                      cr_urls -- all from common/)
                                      |
                                      v
                 +--------------------------------------+
                 |   /config/parental_proxy.db (SQLite,  |
                 |   WAL mode) on the `pp_config` Docker  |
                 |   named volume -- single source of     |
                 |   truth, shared by both containers     |
                 +--------------------+-------------------+
                                      ^
                     reads/writes    (import db, auth, cr_api,
                                      matching -- from common/)
                                      |
                 +--------------------+-------------------+
                 |     dashboard container (Flask)        |
                 |     dashboard.py -- users/domains/      |
                 |     paths/shows/report/settings routes  |
                 |     served by waitress on :8787         |
                 +--------------------+-------------------+
                                      |
                          admin's browser (HTTP Basic auth,
                          DASHBOARD_BIND / DASHBOARD_URL)

  (separate, outbound-only path, no LAN client involved:)
  proxy container's authz_helper.py -> common/series_resolve.py ->
  common/cr_api.py -> https://www.crunchyroll.com/auth/v1/token and
  /content/v2/cms/objects/{ids}  (Crunchyroll's real CMS API, anonymous
  client token, cached in series_cache table)

  dashboard.py also calls common/cr_api.py's series_title() directly, for
  the show-approval UI (best-effort display name lookup).
```

`docker-compose.yml`'s `depends_on: [proxy]` on the dashboard service exists
specifically because the proxy container's `entrypoint.sh` is what `chown -R
proxy:proxy /config` and generates the CA cert on the shared volume before
the dashboard (running as the same `proxy` user) ever opens the database.

---

## 4. The shared SQLite database (`common/db.py`)

- Path: `PP_DB_PATH` env var, default `/config/parental_proxy.db` (see
  `db.py`'s `DB_PATH = Path(os.environ.get("PP_DB_PATH", "/config/parental_proxy.db"))`).
  Both `docker-compose.yml` service blocks set `PP_DB_PATH=/config/parental_proxy.db`
  explicitly, backed by the same `pp_config:` named volume mounted at
  `/config` in both containers.
- `get_conn()` opens with `timeout=5.0`, `isolation_level=None` (autocommit),
  then sets `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, `PRAGMA
  foreign_keys=ON`, and `row_factory = sqlite3.Row`. WAL + busy_timeout is
  what makes concurrent access from many processes (each Squid helper
  invocation is a fresh process, plus the dashboard's Flask/waitress workers)
  safe without a real database server.
- `init_db()` runs the `SCHEMA` string (`CREATE TABLE IF NOT EXISTS ...`) --
  called by every entry point (`squid_helper.run()`, `dashboard.get_db()`,
  `seed_defaults.py main()`), so schema creation is idempotent and doesn't
  depend on ordering between containers.
- Tables: `settings` (key/value, e.g. `local_network`, `block_page_mode`,
  `admin_username`, `admin_password_hash`, `secret_key`), `users`, `domains`
  (`mode` CHECK IN splice/bump/trusted, `kind` CHECK IN generic/crunchyroll,
  `is_global`), `user_domains` (per-user domain grants), `domain_paths`
  (per-domain allowed-path regexes, not per-user), `user_shows` (per-user
  approved Crunchyroll `series_id`s), `series_cache` (object_id -> series_id,
  with `expires_at`), `access_log` (the report page's data source, indexed on
  `ts DESC` and a dedupe composite index).
- There is **no caching layer** in front of the DB anywhere in the request
  path -- every `external_acl_type` line in `squid.conf.template` is declared
  with `ttl=0 negative_ttl=0`, so Squid calls the helper (and thus queries
  SQLite) on every single new connection/request rather than caching a
  verdict. This is why dashboard edits (e.g. approving a show) take effect
  immediately with no proxy restart or reconfigure.

---

## 5. The three domain modes

Set per-row in `domains.mode`, enforced in `common/matching.find_domain()`
lookups and consumed by both `sni_helper.py` and `authz_helper.py`:

- **`splice`** (default for new domains) -- host-only. Squid peeks at the TLS
  ClientHello's SNI (`ssl_bump peek step1`) and, based on
  `sni_helper.py handle_splice`, either splices (passes the encrypted bytes
  through untouched, `ssl_bump splice step2 sni_splice_allowed`) or the
  connection falls through toward `handle_block_page`/terminate. No
  decryption ever happens for an allowed splice-mode site. The report can
  only ever record the domain, never a path, for a `splice` decision --
  nothing downstream ever sees more than the SNI.
- **`bump`** -- fully decrypted (`ssl_bump bump step2 sni_bump`). Once
  bumped, every real HTTP request over that connection is re-evaluated by
  `authz_helper.py`'s `decide()` at the HTTP layer, where path-level and (for
  Crunchyroll) show-level rules apply. Any domain can be switched to `bump`
  from the dashboard to get a per-domain allowed-paths list (same idea as
  v1's `allowed_paths.txt`), independent of the Crunchyroll-specific logic.
- **`trusted`** -- always spliced, never checked, never logged
  (`sni_helper.py handle_trusted` just checks `mode == "trusted"`, no login/
  LAN/user checks at all, and it's the only sni_helper mode with no
  `logging_util.log_access()` call anywhere in its handler). Reserved for
  large-binary CDN traffic (seeded: `gccrunchyroll.com`, `crunchyrollcdn.com`)
  where the actual show-level decision already happened at the earlier
  manifest/token request, which *is* bumped -- there's nothing left to decide
  by the time the CDN request itself happens, so decrypting it would only add
  overhead and log noise for no security benefit.

Every domain is also either **global** (`is_global=1`, granted to every user
automatically -- shared infra like fonts/auth/analytics) or **per-user**
(`is_global=0`, requires a `user_domains` row via `matching.user_has_domain()`).

### Blocked-site experience (`settings.block_page_mode`)

A separate, deployment-wide setting (`terminate` default, or `redirect`),
read by `sni_helper.py handle_block_page()`:
- `terminate`: the SNI-layer catch-all always returns `False`, so
  `squid.conf.template`'s final `ssl_bump terminate step2 all` fires and the
  connection is torn down with no decryption at all -- safe on a device that
  hasn't installed the CA cert, but no explanation shown.
- `redirect`: `handle_block_page()` returns `True`, which (via
  `ssl_bump bump step2 sni_show_block_page`) decrypts *just that one blocked
  connection* so `authz_helper.py`'s normal deny path can serve
  `deny_info ${DASHBOARD_URL}/blocked authz_allowed` (appended to
  `squid.conf` by `entrypoint.sh` only if `DASHBOARD_URL` is set) instead of a
  bare connection failure. Tradeoff: the proxy briefly sees that one blocked
  attempt's real Host+Path, and the device must already trust the CA cert or
  the browser shows a TLS warning instead of a clean page.

---

## 6. End-to-end request flow

### 6a. TLS/SNI layer (every HTTPS connection)

1. Client's device is configured with the proxy IP:3128 and a per-person
   Basic-Auth login. Squid issues the 407 challenge
   (`acl authenticated proxy_auth REQUIRED` / `http_access deny !authenticated`
   in `squid.conf.template`), which invokes
   `auth_param basic program /usr/bin/python3 /opt/parental-proxy/basic_auth_helper.py`.
   `basic_auth_helper.py check(conn, username, password)` looks up
   `users.password_hash` and calls `common/auth.py verify_password()`
   (PBKDF2-SHA256, `hmac.compare_digest`). Note: this helper is invoked with
   `unquote=True, keep_trailing_spaces=True` in `squid_helper.run()` --
   confirmed against a real Squid 5.7 that it *does* percent-encode both
   username and password fields, unlike older Basic-auth folklore (see the
   docstring in `basic_auth_helper.py` and `docs/review-2026-08-28.md` item
   2.8).
2. Client sends `CONNECT host:443`. `http_access allow CONNECT` (qualified
   with `SSL_ports`) lets the tunnel open.
3. `ssl_bump peek step1` -- Squid peeks at the ClientHello without
   decrypting, extracting the SNI hostname.
4. At step2, four `external_acl_type` ACLs (all backed by
   `proxy/sni_helper.py`, invoked with different CLI args -- `bump`,
   `trusted`, `splice`, `block_page` -- see `HANDLERS` dict and `main()`) are
   checked in the fixed order written into `squid.conf.template`'s "SSL BUMP
   DECISION CHAIN" section (first match wins):
   - `sni_trusted` -> `handle_trusted()` -> splice if `mode == 'trusted'`
   - `sni_bump` -> `handle_bump()` -> bump if `mode == 'bump'`
   - `sni_splice_allowed` -> `handle_splice()` -> splice if `mode == 'splice'`
     AND the user is authenticated, inside the configured LAN
     (`matching.ip_in_configured_lan()`), and either the domain `is_global`
     or `matching.user_has_domain()` returns true
   - `sni_show_block_page` -> `handle_block_page()` -> bump-for-denial only if
     `block_page_mode == 'redirect'`
   - final catch-all: `ssl_bump terminate step2 all`
   Each handler calls `matching.find_domain(conn, sni)`, which iterates
   `domains` rows in `id` order and regex-matches via
   `matching._domain_regex()` -- an anchored domain-suffix match
   (`r"(?:^|\.)(?:" + pattern + r")\Z"`), specifically chosen so a stored
   pattern like `jsdelivr\.net` cannot be satisfied by `evil-jsdelivr.net` or
   `jsdelivr.net.attacker.example` (this was a real bypass before the anchor
   was added).

### 6b. HTTP layer (only reached for `bump`-mode connections, once decrypted)

5. Once `ssl_bump bump` fires, `http_access allow CONNECT step2` only covers
   the TLS negotiation itself -- **not** the real decrypted request that
   follows (this distinction is load-bearing: a bare `http_access allow
   step2`, without `CONNECT`, previously bypassed all per-domain/path/show
   enforcement, see squid.conf.template's inline comment and
   `docs/review-2026-08-28.md`). The actual decrypted HTTP request instead
   falls through to `http_access allow authz_allowed`, backed by
   `external_acl_type authz_check` -> `proxy/authz_helper.py decide()`.
6. `decide(conn, login, client_ip, dst, path, _data)`:
   - fails closed if not authenticated or outside the configured LAN
     (`matching.ip_in_configured_lan()`)
   - `matching.find_domain()` on the decrypted `Host` (`%DST`, host:port
     split via `_split_host_port()`); denies as `unknown_domain` or
     `not_bump_mode` if the domain isn't a `bump`-mode row (this is also how
     a `redirect`-mode block-page connection, which got bumped only to be
     denied, is actually denied here)
   - checks `domain.is_global` or `matching.user_has_domain()`
   - **if `domain.kind == 'crunchyroll'`**: delegates to
     `_decide_crunchyroll()` (see 6c below)
   - **otherwise**: `matching.path_allowed(conn, domain_id, path)` against
     `domain_paths` rows, but only enforced if the domain has *any*
     configured paths at all (`_has_any_path_rules()`) -- a domain with zero
     path rows allows any path once the domain-level check passes.
   - every branch calls `logging_util.log_access()` with a `reason` string
     (`global_domain`, `user_domain`, `path_not_allowed`, `domain_not_assigned`,
     `unknown_domain`, `not_bump_mode`, `outside_lan`, etc.) -- these reason
     strings are what the dashboard's Report page displays and what makes an
     entry "Approve"-able.

### 6c. Crunchyroll-specific show resolution

`authz_helper._decide_crunchyroll(conn, user, login, hostname, path, domain)`:
1. Builds the full URL and calls `common/cr_urls.py classify(url)`, which
   pattern-matches (no network I/O, no state) against
   `SERIES_URL_RE`/`WATCH_URL_RE`/`PLAYBACK_URL_RE`/`CMS_OBJECTS_URL_RE` and
   returns a `ClassifiedRequest(kind, ids)`.
   - `CMS_OBJECTS` -> always allowed (metadata-only, matches v1 behavior).
   - `BLOCKED_SHAPE` -> always denied (fail-closed: looks like a guarded
     route -- `/watch/`, `/playback/`, `/cms/objects/` -- but didn't match a
     known-safe shape).
   - `OTHER` -> falls back to the domain's `domain_paths` allowlist exactly
     like a non-Crunchyroll bump-mode domain would.
   - `SERIES_PAGE` -> checks `matching.user_has_show()` directly against the
     URL's own series id.
   - `WATCH_PAGE` / `PLAYBACK` -> the URL only carries an *object* id (an
     episode or playback-token id), not a series id, so it must be resolved
     first via `common/series_resolve.py resolve_series_ids(conn, request.ids)`.
2. `series_resolve.resolve_series_ids()` is cache-first against the
   `series_cache` table (`_cache_get`/`_cache_put`): a hit returns
   immediately; a miss calls `cr_api.SeriesResolver().resolve()` (which does
   the actual network call to `https://www.crunchyroll.com/content/v2/cms/objects/{ids}`,
   authenticated with an anonymous client-credentials Bearer token from
   `TokenManager` against `https://www.crunchyroll.com/auth/v1/token`). A
   successful resolution is cached for `POSITIVE_TTL_SECONDS` (30 days, since
   an object's parent series never changes); an explicit miss is cached
   negatively for `NEGATIVE_TTL_SECONDS` (1 hour). If the resolver raises
   `cr_api.ResolutionError` (network down, or the hardcoded anonymous client
   id has been rotated -- see `cr_api.py`'s `ANON_BASIC_CREDENTIAL` comment),
   `resolve_series_ids()` falls back to serving a still-present *positive*
   cache entry even past its TTL (`allow_stale=True`) rather than break
   playback of an already-approved show; an id with no positive history at
   all returns `None` from the whole function, which `decide()` treats as
   `resolution_failed` and denies (fail-closed contract, same as v1).
3. Each resolved `series_id` is checked against `matching.user_has_show()`
   (`user_shows` table, `series_id` normalized upper-case), and every
   individual object id gets its own `logging_util.log_access()` call with
   `series_id`/`series_name` attached, so a report entry for a blocked show
   is individually approvable.

`common/cr_api.py` is called from two places: `series_resolve.py` (the
proxy-side resolution path above) and `dashboard.py` directly, for
`series_title(series_id)` -- a best-effort display-name lookup used by the
show-approval UI, tolerant of any failure (returns `None`, dashboard falls
back to the raw id).

---

## 7. Dashboard (`dashboard/dashboard.py`)

Single-file Flask app (~1100 lines), imports `auth`, `cr_api`, `db`,
`matching` from `common/` (via `sys.path.insert(0, str(Path(__file__).parent))`,
since the Dockerfile copies `common/*.py` flat into `/app/` alongside
`dashboard.py`). Served by `waitress.serve()` in `main()`, not Flask's dev
server, bound to `DASHBOARD_HOST`/`DASHBOARD_PORT` (default `127.0.0.1:8787`,
overridden to `0.0.0.0` by `docker-compose.yml`, with host exposure actually
controlled at the Docker port-mapping level via `DASHBOARD_BIND`).

Route groups (see the numbered `# ====` section banners in the file):
- **Admin auth**: `bootstrap_admin()` seeds `admin_username`/
  `admin_password_hash` into `settings` from `DASHBOARD_USER`/
  `DASHBOARD_PASSWORD` env vars on first run only (prints a generated
  password to stderr if unset); `require_admin` decorator + `_check_admin_auth()`
  gate every route except `/ca-cert` and `/blocked` (both deliberately public
  -- a CA cert isn't a secret, and a blocked kid needs to see the block page
  without logging in as admin).
- A `before_request` hook (`_reject_cross_origin_writes`) is a lightweight
  CSRF guard: rejects any POST/PUT/PATCH/DELETE whose Origin/Referer host
  doesn't match `request.host` (relevant because Basic-Auth credentials are
  attached by the browser automatically, unlike a cookie session that a
  SameSite policy could protect).
- **Users** (`/users`, `/users/add`, `/users/delete`, `/users/reset-password`,
  `/users/<id>`): CRUD on the `users` table, `add_user()` calls
  `auth.hash_password()`.
- **Shows** (`/shows/add`, `/shows/remove`, `parse_series_url()`): per-user
  `user_shows` management, including the "paste a Crunchyroll URL" flow that
  parses out a series id.
- **Domains** (`/domains`, `/domains/add`, `/domains/add-url`,
  `/domains/delete`, `/domains/<id>`, `/domains/update`,
  `/domains/toggle-user`, `/domains/paths/add`, `/domains/paths/delete`):
  CRUD on `domains`, `user_domains`, and `domain_paths`; `path_to_pattern()`
  converts a pasted URL path into a stored regex pattern.
- **Report** (`/report`, `/report/approve`): reads `access_log`, and
  `approve_from_report()` implements the one-click-approve flow -- for a
  domain-level denial it creates a `user_domains` row (creating the `domains`
  row itself first if it never existed, scoped to that one user); for a
  show-level denial it creates a `user_shows` row.
- **Settings** (`/settings`, `/settings/local-network`,
  `/settings/block-page-mode`, `/settings/admin`): editable
  `local_network`, `block_page_mode`, and admin credentials -- all live
  edits to the `settings` table, no container restart needed.
- `/ca-cert`: unauthenticated `send_file()` of `PP_CA_CERT_PATH`
  (`/config/ssl_cert/ca_cert.pem`) for device onboarding.
- `/blocked`: unauthenticated, the target of `deny_info` for `redirect`-mode
  block pages.

---

## 8. Container startup (`proxy/entrypoint.sh`, `proxy/Dockerfile`)

1. `mkdir -p /config /config/ssl_cert`, then `chown -R proxy:proxy /config`
   -- both containers run helper/dashboard code as the `proxy` user (Debian
   uid 13) so the shared SQLite file (+ `-wal`/`-shm` sidecars) stays
   writable by both; this also repairs a volume left root-owned by an older
   build.
2. Runs `defaults/seed_defaults.py` and an inline Python heredoc
   (`db.set_setting_if_absent` for `local_network`/`block_page_mode`) as the
   `proxy` user via `runuser` if available, so the DB file it may create is
   correctly owned.
3. Generates the SSL-Bump CA cert (`openssl req -x509 ... -addext
   basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign`)
   into `$SSL_DIR` only if missing -- this is the cert devices must trust,
   and its private key never leaves the container/volume.
4. Initializes Squid's `security_file_certgen` cert-signing DB
   (`/var/lib/squid/ssl_db`) if missing -- `squid-openssl` doesn't pre-create
   `/var/lib/squid` itself, so this directory must be created before
   `security_file_certgen -c` runs or it fails.
5. Copies `squid.conf.template` to `/etc/squid/squid.conf`, then
   conditionally appends `deny_info ${DASHBOARD_URL}/blocked authz_allowed`
   if `DASHBOARD_URL` is set (this line can't be templated with `envsubst`
   directly because an empty `DASHBOARD_URL` would still need the whole line
   dropped, not substituted empty).
6. `exec squid -N -f /etc/squid/squid.conf`.

`defaults/seed_defaults.py`'s `seed()` is fully idempotent (`INSERT OR
IGNORE` everywhere), safe to run on every container start against an
already-configured database -- it seeds `GLOBAL_SPLICE_DOMAINS` (infra like
`google.com`, `gstatic.com`, `jsdelivr.net`, etc., all `splice`/global),
`TRUSTED_DOMAINS` (`gccrunchyroll.com`, `crunchyrollcdn.com`, both
`trusted`/global), the `crunchyroll.com` domain itself (`bump`,
`kind='crunchyroll'`, global), and `CRUNCHYROLL_PATHS` attached to that
domain's `domain_paths`. Notably it does *not* seed a `crunchyrollsvc.com`
domain -- confirmed against Crunchyroll's live webpack bundle that the
`cr-play-service.*.crunchyrollsvc.com` host is dev-only and real production
playback goes through `www.crunchyroll.com/playback`, already covered.

---

## 9. Non-obvious design decisions worth knowing before changing this code

- **`%DATA` must be declared in every `external_acl_type` FORMAT line even
  though every handler ignores it.** Squid always appends a `%DATA` field to
  the line it sends a helper unless the FORMAT already includes one. None of
  the `acl ... external ...` lines in `squid.conf.template` pass a static
  argument, so `%DATA` is always literally `"-"` -- but it is still sent,
  making the line one field longer than its `%macro` count suggests.
  Omitting it previously meant every helper's `field_count` (in
  `squid_helper.run()`) was off by one, so every request was rejected as a
  malformed line and silently fell through to the terminate-all catch-all.
  This is why every handler signature in `sni_helper.py` and
  `authz_helper.py` ends with an unused `_data: str = "-"` parameter.
- **`http_access allow CONNECT step2` must be qualified with `CONNECT`, not
  left bare.** Squid's `at_step` state does not reset after `ssl_bump`
  reaches step2 -- the first decrypted HTTP request that follows a bump
  decision is *also* evaluated with step2 still true. A bare `http_access
  allow step2` therefore grants that real request unconditional access
  before `authz_allowed` is ever reached, bypassing all per-domain/path/show
  enforcement for every bump-mode request including Crunchyroll. Confirmed
  against a real Squid instance (see `docs/review-2026-08-28.md`).
- **CDN domains are `trusted` (spliced, unchecked, unlogged) rather than
  `bump`**, because the actual show-level authorization decision already
  happened at the earlier manifest/playback-token request (which *is*
  bumped) -- by the time raw video bytes are being requested from
  `crunchyrollcdn.com`/`gccrunchyroll.com`, there's nothing left to decide
  per-user, so decrypting it would only add overhead and log noise.
- **Domain patterns are anchored regexes, not substrings**
  (`matching._domain_regex()`): `r"(?:^|\.)(?:" + pattern + r")\Z"`. Without
  this anchor, `re.search` treated every stored pattern as an unanchored
  substring match, so an FQDN merely *containing* an allowed string (e.g.
  `evil-jsdelivr.net` against the seeded `jsdelivr\.net`) would slip through.
- **`domain_paths` is per-domain, not per-user.** It's defense-in-depth
  against unrecognized endpoint shapes on a `bump`-mode domain (same idea as
  v1's flat `allowed_paths.txt`), not a per-kid permission -- per-kid
  permission for Crunchyroll is expressed at the show level
  (`user_shows`), and for any other domain at the domain level
  (`user_domains`).
- **A `bump`-mode domain with zero configured `domain_paths` rows allows any
  path** once the domain-level check passes (`_has_any_path_rules()`) --
  path restriction is opt-in per domain, so admins only curate paths for
  domains where it actually matters.
- **`access_log` dedupe includes `path` with its query string stripped**
  (`logging_util.log_access()`), and treats `series_id` as part of the
  dedupe key using SQLite's null-safe `IS`. This means a path-less SNI-layer
  entry and a later path-bearing HTTP-layer entry for the same event are
  different dedupe keys, so a richer, later entry is never hidden behind an
  earlier, less informative one within the same 5-minute window.
- **`series_resolve.py` never deletes an expired positive cache row before
  attempting a live re-resolve.** `_cache_get()` reports an expired positive
  entry as a miss (so a fresh resolve is attempted) but leaves the row in
  place, specifically so `resolve_series_ids()`'s `allow_stale=True` fallback
  can still serve it if the live resolver call then fails. An earlier version
  of this logic deleted the stale entry first, which broke the fallback it
  was supposed to support (see README's "Testing notes" section, S2.6).
- **Everything is fail-closed by convention**: an unrecognized/malformed
  Squid protocol line returns `ERR` (`squid_helper.run()`'s `except Exception`
  wraps any handler crash the same way), `cr_urls.classify()` returns
  `BLOCKED_SHAPE` rather than `OTHER` for anything that looks like a guarded
  route but doesn't match a known-safe shape, and
  `series_resolve.resolve_series_ids()` returns `None` (which
  `authz_helper.decide()` treats as a hard deny) for any object id with no
  usable cache history at all.
