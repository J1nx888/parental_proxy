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
3. **Reporting, with one-click approve -- and the kid can ask.** Every
   allow/block decision is logged with who, what, and when, filterable by
   kid and by date range (1/7/14/30 days) with at-a-glance graphs. A
   blocked entry gets Approve / Approve for everyone / Dismiss buttons, and
   the block page itself has a "Request approval" button -- a kid doesn't
   need to know the dashboard exists for their request to show up as a
   pending alert an admin can act on immediately.

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
denied. And either way, clicking "Approve" (or "Approve for everyone") on
a report entry works even for a domain that was never configured before
-- it gets created automatically, scoped to that one user or made global.

## Crunchyroll specifically

Ships pre-configured as a global `bump`-mode domain. On top of the normal
domain/path checks, watch/playback/series requests get resolved to their
parent show via Crunchyroll's CMS API (cached in the database) and checked
against each user's individually-approved show list. This logic is
Crunchyroll-specific and stays that way -- nothing else has an API to
resolve "this URL belongs to this show," so it's not a generic feature.

## The dashboard

Server-rendered (Flask + Jinja2, no separate frontend build) but styled as
a proper small app: cards, light/dark mode following the OS setting,
responsive down to phone width, and installable as a PWA (Add to Home
Screen) -- that last part needs a secure context (`localhost`, or HTTPS
once that's set up), so it won't offer to install over plain HTTP to a LAN
IP, but everything else works everywhere.

**Report page:**

- A stat strip (total / allowed / blocked / % blocked) where **Allowed**
  and **Blocked** are clickable -- click one to filter the whole page,
  graphs included, down to just that outcome. A **Clear filters** button
  appears whenever a filter is active, to jump back to the default view.
- A **Filter** control for kid, allowed/blocked, and a date range (1, 7,
  14, or 30 days) that applies to everything below it at once -- the
  totals, both graphs, and the activity table never disagree about what
  window they're showing.
- Two graphs (Chart.js, vendored locally -- no CDN dependency): activity
  over the selected date range, and top domains by request count.
- A **Pending approval requests** card at the top, independent of whatever
  filter is set, listing anything a kid has asked to have approved (see
  below) with **Approve**, **Approve for everyone**, and **Dismiss** for
  each.

**Kid-initiated requests:** the page a kid sees when something's blocked
now has a "Request approval" button. They don't need to know the
dashboard exists or where the Report page is -- clicking it creates a
pending request an admin sees immediately, instead of the only path being
someone noticing it later in the log.

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
3. **Approve shows/sites** ahead of time (from their user page / the
   Domains page), reactively from the Report page as blocks show up, or
   let the kid ask -- the block page has its own "Request approval"
   button that surfaces a pending request on the Report page without
   anyone needing to go looking for it. Approving works even for a site
   that was never configured before -- it gets created automatically,
   either scoped to that one user or, via "Approve for everyone," to the
   whole household.

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
  static/         CSS design system, vendored Chart.js, PWA manifest +
                  service worker, generated icon set
  dev_server.py   local-only launcher for previewing the dashboard
                  outside Docker (not used by the built image)
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
  blocked site and a blocked show (including "Approve for everyone"),
  confirmed to actually grant access afterward (not just show a success
  message).
- The full kid-initiated request lifecycle: `/blocked`'s "Request
  approval" button through to an admin's Approve / Approve for everyone /
  Dismiss, including that Dismiss clears the request without granting
  anything. The Report page's kid/status/date-range filtering (applied
  consistently to the stat strip, both graphs, and the activity table),
  the clickable Allowed/Blocked stat links, and Clear filters.
- `seed_defaults.py` idempotency (run twice, no duplicates/errors), plus a
  regression case for the `crunchyrollcdn.com` mode bug and confirmation
  that `crunchyrollsvc.com` -- confirmed dev-only in Crunchyroll's own
  production config, and removed from the codebase -- is never seeded.
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

**Update (2026-08-28): all of this has now actually been run**, against a
disposable Debian 12 VM with Docker, and separately on a real Android
tablet -- `docker compose up --build`, real proxy auth, real per-site and
per-show enforcement, CA trust, and real Crunchyroll playback (one show
approved and playing, a second, unapproved show correctly blocked). Five
real bugs were found and fixed in the process (Debian's plain `squid`
package lacking SSL-Bump support, a missing directory needed for cert
generation, a Squid protocol quirk that silently broke every
`external_acl_type` decision, an `http_access` ordering bug that bypassed
per-show enforcement entirely, and a wrong assumption about Basic-auth
percent-encoding) -- see `docs/review-2026-08-28.md` for the full writeup
of each. The Squid `external_acl_type`/`ssl_bump`/Basic-Auth combination
this project depends on is confirmed working as designed.

## Backing up / moving to another machine

Same idea as v1, different filename:

```
docker run --rm -v <project-dir-name>_pp_config:/config -v "$PWD":/backup \
  alpine tar czf /backup/pp-config-backup.tar.gz -C /config .
```

This includes the CA private key (so devices don't need to re-trust a new
cert after a restore) and the full SQLite database (every user, permission,
and log entry).
