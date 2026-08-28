# Parental Proxy v2

A from-scratch rewrite of the v1 Crunchyroll whitelist proxy. Same core
idea (an SSL-bumping Squid proxy enforcing what gets through), rebuilt
around three new features:

1. **Everything is configured from the dashboard** -- no files to hand-edit,
   no `squid -k reconfigure`, no restart for any rule change. Even the LAN
   IP range and the dashboard's own admin password are editable from the
   web UI after first boot (seeded from `.env` only once, on first run).
2. **Multiple people, each with their own rules.** Every person gets their
   own login (configured in their device's proxy settings), and site/show
   permissions are assigned per person -- kid1 can reach `xyz.com` but not
   `abc.com`; kid2 can reach both.
3. **Reporting, with one-click approve.** Every allow/block decision is
   logged with who, what, and when. A blocked entry in the report has an
   "Approve for this user" button that grants it immediately.

## How it decides what to do with a site

Every domain has a **mode**, set from the dashboard:

- **`splice` (default)** -- host-only. Squid peeks at the TLS handshake
  (SNI), checks whether this user may reach that domain, and either passes
  the encrypted connection through untouched or terminates it. No
  decryption, no path-level detail, cheap. This is what "kid1 can reach
  xyz.com" means in practice.
- **`bump`** -- fully decrypted, for domains where path-level rules matter.
  Crunchyroll ships in this mode with show-level enforcement built on top
  (see below); you can put any other domain in `bump` mode too and get a
  per-domain allowed-paths list, same idea as v1's `allowed_paths.txt`.
- **`trusted`** -- always spliced, never checked, never logged. For known
  large-binary CDN traffic where there's nothing per-user to decide (the
  actual show-level decision already happened at the manifest/token
  request, which *is* bumped).

Every domain is also either **global** (every user gets it automatically --
for shared infrastructure like fonts, auth providers, analytics) or
**per-user** (each person needs an explicit assignment).

**Blocked-site experience is configurable (Settings → Blocked-site
experience):**

- **"Just fail the connection" (default).** Never decrypts a blocked
  attempt. Safe for any device, including one that hasn't installed the CA
  certificate yet -- the kid just sees a broken-looking connection with no
  explanation, and the report only ever has the domain (not the path) for
  `splice`-mode blocks.
- **"Show a friendly page."** A blocked `splice`-mode connection gets
  bumped -- decrypted -- just for that one connection, purely so a real
  deny page can be served instead of a bare connection failure (the same
  mechanism `bump`-mode domains already use). Two tradeoffs: the proxy
  briefly sees that blocked attempt's actual Host+Path before denying it
  (which it otherwise never would for a `splice`-mode site), and **this
  requires the CA certificate to already be trusted on the device**. If
  it isn't, decrypting the connection means presenting a certificate the
  device doesn't trust -- the browser shows a security warning
  ("connection not private") instead of a clean block message, which is a
  worse experience than the plain failure it replaces. Only turn this on
  after confirming certificate trust works (if a `bump`-mode site like
  Crunchyroll already loads cleanly on a device, that device is fine).
  This is a single setting for the whole deployment, not per-device.

Either way, allowed `splice`-mode traffic is never touched -- this setting
only affects what happens to connections that were already going to be
denied. And either way, clicking "Approve" on a report entry works even
for a domain that was never configured before -- it gets created
automatically, scoped to that one user.

## Crunchyroll specifically

Ships pre-configured as a global `bump`-mode domain. On top of the normal
domain/path checks, watch/playback/series requests get resolved to their
parent show via Crunchyroll's CMS API (cached in the database) and checked
against each user's individually-approved show list. This logic is
Crunchyroll-specific and stays that way -- nothing else has an API to
resolve "this URL belongs to this show," so it's not a generic feature.

## Quickstart

Requires [Docker](https://docs.docker.com/get-docker/) with the `docker
compose` plugin.

```
git clone https://github.com/J1nx888/crunchyroll_parentalcontrols
cd crunchyroll_parentalcontrols   # or wherever this v2 tree lives
./setup.sh
```

The wizard asks for your LAN CIDR, an admin username/password, whether to
expose the dashboard beyond this machine, and whether you want a friendly
block page (needs this machine's LAN IP). Then it builds and starts both
containers.

## Setting up a person

1. **Dashboard → Users → Add user.** Pick a username and password -- this is
   what goes in that person's device proxy settings, not the dashboard
   login.
2. **On that person's device:** install the CA certificate (Users page has
   a download link -- same certificate for everyone, no per-user cert) and
   set the device's Wi-Fi/network proxy to this machine's IP, port `3128`,
   with that person's username/password.
3. **Approve shows/sites** either ahead of time (from their user page /
   the Domains page) or reactively from the Report page as blocks show up
   -- approving from the report works even for a site that was never
   configured, it gets created automatically, scoped to that one user.

Setting `DASHBOARD_URL` in `.env` gets you a real, project-branded block
page. Without it, a blocked `bump`-mode or redirect-mode `splice` attempt
still gets Squid's own generic "Access Denied" page rather than nothing --
`DASHBOARD_URL` is a nicer version of that, not a requirement for it to
work at all.

Per-device certificate trust steps (Windows/macOS/iOS/Android/Chromebook)
are the same as v1 -- see the "Setting up a device" walkthrough in that
project's history if you need the exact menus; the mechanism (trust one CA
cert, set one proxy) hasn't changed.

**Native apps:** same caveat as v1 -- this filters the *website*. A native
app doing certificate pinning will fail to connect rather than being
filtered, since SSL bump can't get around pinning without modifying the
app itself (which this project doesn't do). Check the app's own
parental-control features or your OS's app-blocking controls if the app
matters to you.

## What's genuinely new vs. v1, and what carried over

**Carried over:** the SSL-bump approach itself, the Crunchyroll CMS
resolution logic (`cr_api.py` is untouched), the show-approval-by-pasting-
a-URL flow, the defense-in-depth idea of a path allowlist independent of
the show-level check, the CA-cert-download-from-the-dashboard onboarding
flow.

**New:** per-user logins and permissions (Squid Basic Auth against a
`users` table), SQLite instead of JSON/text files, one unified decision
engine instead of split static-file + script logic, the mode system
(splice/bump/trusted) making the "decrypt everything or nothing" choice
explicit and per-domain instead of an implicit whole-project default, the
access log + reporting UI, click-to-approve, and live-editable settings
(LAN CIDR, admin credentials) instead of baked into `squid.conf` at
container start.

**Behavior change worth knowing:** v1 was already default-deny (a fixed
final `deny all` in squid.conf) -- v2 doesn't change that philosophy, it
just makes the allowlist per-person instead of shared. What *does* change
is how much traffic gets decrypted: v1 mostly spliced non-Crunchyroll
domains (never inspected them), and only fully decrypted Crunchyroll
itself. v2 defaults new domains to `splice` too (same low-decryption
posture), but reporting and per-path rules on any *other* domain now
require deliberately switching that domain to `bump` mode -- that's a
choice you make per-domain, not a blanket change.

## Architecture

```
common/            shared by every component
  db.py              SQLite schema + connection helper
  auth.py            password hashing (PBKDF2, stdlib-only)
  cr_api.py           Crunchyroll CMS API client (unchanged from v1)
  cr_urls.py          Crunchyroll URL classification (adapted from v1)
  series_resolve.py   DB-backed object-id -> series-id cache
  matching.py         domain/path pattern matching, LAN CIDR check
  logging_util.py     deduped access-log writer

proxy/
  basic_auth_helper.py  Squid `auth_param basic` helper -- per-person login
  sni_helper.py          Squid `external_acl_type` for the ssl_bump decision
                          (bump/trusted/splice, checked via SNI)
  authz_helper.py        Squid `external_acl_type` for the HTTP-layer
                          decision on bump-mode domains (paths + shows)
  squid.conf.template
  entrypoint.sh
  Dockerfile

dashboard/
  dashboard.py    Flask app: users, domains, per-domain paths, per-user
                  shows, report + approve, settings, CA cert download
  Dockerfile

defaults/
  seed_defaults.py   idempotent first-run seeding (infra domains,
                      Crunchyroll domain + paths, trusted CDN list)
```

One database (`/config/parental_proxy.db`, SQLite, WAL mode) shared by both
containers via a Docker volume. Every helper script and the dashboard open
it directly -- there's no caching layer to go stale, since `ttl=0` on every
`external_acl_type` means Squid re-checks with the helper on every new
connection rather than caching a verdict.

## Testing notes

This sandbox has no Docker daemon, so `docker compose up --build` itself
hasn't been run. What *has* been tested is a real, runnable suite under
[`tests/`](tests/README.md) (`pip install pytest && pytest`, no Docker or
network needed -- see that file for what's covered):

- All three Squid helper scripts (`basic_auth_helper.py`, `sni_helper.py`
  in all three modes, `authz_helper.py`), including the shared stdin/stdout
  protocol loop itself -- correct auth, correct per-user domain/show
  decisions, correct LAN/authentication fail-closed behavior, correct
  access logging with dedupe.
- The full dashboard workflow via Flask's test client: admin auth, user
  CRUD, domain CRUD, per-user domain assignment, the CSRF/cross-origin
  guard, and -- specifically -- the click-to-approve flow for both a
  blocked site and a blocked show, confirmed to actually grant access
  afterward (not just show a success message).
- `seed_defaults.py` idempotency (run twice, no duplicates/errors), plus
  regression cases for the `crunchyrollcdn.com` mode bug and the missing
  `crunchyrollsvc.com` playback host.
- `common/matching.py` domain-suffix anchoring (including the substring/
  suffix bypass cases), path matching, and the LAN CIDR check.
- `common/cr_urls.py` request classification and `common/auth.py` password
  hashing, including malformed/tampered-input edge cases.
- `common/cr_api.py` (token refresh/retry-on-401/error handling) and
  `common/series_resolve.py`'s cache (positive/negative TTL, and the S2.6
  stale-on-error fallback -- writing this test caught a real bug where the
  stale entry was being deleted before the fallback could serve it; now
  fixed, see `docs/review-2026-08-28.md`).

Separately (not part of the `pytest` suite, just a manual syntax check):
Squid config template, Dockerfiles, docker-compose.yml, and both shell
scripts for syntax validity.

**Not tested here, and worth doing before relying on this:** an actual
`docker compose up --build` and a real device going through the full flow
(login prompt, CA trust, Crunchyroll playback, a second user seeing
different sites). The Squid config in particular -- `external_acl_type`
with `%ssl::>sni` at ssl_bump step2, `ssl_bump terminate`, and Basic Auth
combined with ssl-bump -- are all individually standard, documented Squid
features, but this exact combination hasn't been run against real Squid.
If step2's SNI-based routing doesn't behave exactly as expected on your
Squid version, the most likely symptom is everything falling through to
`ssl_bump terminate step2 all` (fails closed -- sites simply won't load
rather than silently passing through unfiltered), which is a safe failure
mode to debug from.

## Backing up / moving to another machine

Same idea as v1, different filename:

```
docker run --rm -v <project-dir-name>_pp_config:/config -v "$PWD":/backup \
  alpine tar czf /backup/pp-config-backup.tar.gz -C /config .
```

This includes the CA private key (so devices don't need to re-trust a new
cert after a restore) and the full SQLite database (every user, permission,
and log entry).
