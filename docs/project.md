# Parental Proxy v2

> A self-hosted, SSL-Bumping Squid proxy with a Flask admin dashboard that enforces per-person website/show permissions on a home network, with a full audit log and one-click "approve" workflow.

## Overview

Parental Proxy v2 is a from-scratch rewrite of an earlier Crunchyroll-only whitelist proxy. It's built around three ideas: everything is configured live from a web dashboard (no config files to hand-edit, no Squid restarts for rule changes); every person in the household has their own site/show permissions, applied per-request based on which assigned device their traffic comes from (not a per-request login, since 2026-08-30 -- see below); and every allow/block decision is logged with who/what/when, with a one-click "Approve" action on any blocked report entry.

The system is two Docker containers sharing one SQLite database: a Squid proxy container that does the actual traffic filtering (via `external_acl_type` helper scripts, not Squid's native ACL language), and a Flask dashboard container that's the only place an admin ever touches configuration. Squid's SSL-Bump feature lets the proxy selectively decrypt specific domains ("bump" mode) to enforce path-level and show-level rules (e.g. which Crunchyroll series a kid may watch), while most domains stay in cheaper, privacy-preserving "splice" mode (SNI-only, never decrypted, domain-level allow/deny only).

The project is currently mid-way through a larger v2 redesign aimed at replacing a commercial whole-home filter (Bark Home): adding a DNS-based filtering tier for devices that can't run a proxy at all (game consoles, smart TVs), a captive-portal-style forced-enrollment flow, and expanding the Crunchyroll-style show-level filtering pattern to YouTube channels. None of that is built yet — see [`../RoadMap.md`](../RoadMap.md) for the full plan. **This documentation describes the system as it exists in code today**, not the in-progress redesign.

## How it works

A bump-enabled device's port 80/443 traffic reaches Squid's intercept ports (`http_port 3129 intercept` / `https_port 3130 intercept ssl-bump`) via NAT redirection -- no per-device proxy configuration, just a trusted CA certificate (see `docs/architecture/overview.md` for what does that redirecting; it's Phase 3's interception layer, outside this repo's proxy/dashboard tree). At the TLS layer, Squid peeks at the SNI hostname and asks `proxy/sni_helper.py` (one of four modes: `bump`/`trusted`/`splice`/`block_page`) whether to decrypt, splice through untouched, or terminate — consulting `common/matching.py` and the shared SQLite database for the domain's configured mode and the requesting device's assigned user's permissions (identity resolved from the client's source IP via `common/device_identity.py` → `device_bindings` → `devices.user_id`, replacing a per-request Basic-Auth login as of 2026-08-30). If a domain is in `bump` mode, the now-decrypted HTTP-layer request is additionally checked by `proxy/authz_helper.py`, which for Crunchyroll resolves the actual show being requested (`common/cr_urls.py` → `common/series_resolve.py` → `common/cr_api.py`) and checks it against that user's approved-shows list. Every decision is logged via `common/logging_util.py`. The Flask dashboard (`dashboard/dashboard.py`) reads and writes the same database to manage users, domains, devices, per-user assignments, and the report/approve workflow.

## Repository layout

```
common/        Shared Python modules used by BOTH containers (flat-copied into each image)
  auth.py            password hashing/verification (PBKDF2)
  db.py              SQLite schema (SCHEMA string) + init_db()
  matching.py        domain/user/LAN-permission lookup logic
  device_identity.py resolve_device(conn, client_ip) + resolve_user_for_device(conn, device)
                      -- Squid's identity source since 2026-08-30 (split into two
                      functions 2026-08-31, see docs/security/overview.md §3)
  squid_helper.py    shared external_acl_type request/response protocol plumbing
  logging_util.py    deduped access-log writer
  cr_urls.py         Crunchyroll URL classification (playback/series/season/episode)
  series_resolve.py  resolves a Crunchyroll URL's ids to a series_id (with caching)
  cr_api.py          Crunchyroll CMS API client (anonymous token flow)

proxy/         The Squid container
  squid.conf.template   Squid config: intercept-mode ports, ssl_bump rules, http_access,
                         external_acl_type wiring (see RoadMap.md's intercept-mode section)
  sni_helper.py          external_acl_type handler for ssl_bump step2 (bump/trusted/splice/block_page)
  authz_helper.py        external_acl_type handler for the decrypted HTTP layer
  entrypoint.sh          generates the CA cert, fixes volume ownership, starts Squid
  Dockerfile             squid-openssl (NOT plain squid -- required for SSL-Bump)

dashboard/     The Flask admin container
  dashboard.py       every route + inline Jinja2 templates (single file, ~1100 lines)
  Dockerfile         flattens common/*.py + dashboard.py into one /app directory

defaults/
  seed_defaults.py   idempotent first-run seed data (global domains, Crunchyroll config)

tests/         Tier-1 pytest suite (358 tests as of 2026-08-30, no Docker/network required)
docs/          This documentation
```

## Documentation

| Section | Contents |
|---------|----------|
| [Architecture](architecture/overview.md) | Components, tech stack, component diagram, the three domain modes, end-to-end request flow, non-obvious design decisions |
| [Dashboard routes](dashboard/routes.md) | Every Flask route, the template constants, CSRF/`flash_redirect` patterns, how to add a new page |
| [Database schema](database/schema.md) | Every table (including the Phase 2/3 devices/groups/identity tables), columns/constraints/relationships, every `access_log.reason` value enumerated, seed data |
| [Deployment](deployment/setup.md) | Local setup (`setup.sh` and manual), every env var, the CA cert, port mappings, CI |
| [G1 runbook](deployment/g1-runbook.md) | Real-network ARP interception validation — the one remaining gate before replacing Bark Home |
| [Security](security/overview.md) | Auth/CSRF/password hashing, Squid's device-identity model (intercept mode, since 2026-08-30), the CA/bump trust model, known gaps (no rate limiting), LAN-only scoping |
| [Testing](testing/overview.md) | How to run the suite, key fixtures, mutation-testing verification process, how to add a test |

## Quick start

```bash
# Interactive setup (prompts for LAN CIDR, admin credentials, builds + starts both containers)
./setup.sh

# Or manually:
cp .env.example .env    # edit LOCAL_NETWORK, DASHBOARD_USER, DASHBOARD_PASSWORD, etc.
docker compose up -d --build

# If DASHBOARD_PASSWORD was left blank, retrieve the auto-generated one:
docker compose logs dashboard

# Run the test suite:
pip install -r requirements-dev.txt -r dashboard/requirements.txt
pytest
```

Dashboard defaults to `http://127.0.0.1:8787` (not LAN-reachable until `DASHBOARD_BIND` is changed). Squid listens on its intercept ports (3129/3130) but no device configures a proxy setting at all -- traffic reaches them via Phase 3's NAT redirection once deployed; the only per-device manual step is trusting the CA cert.

## Key technical decisions

- **`external_acl_type` helper scripts instead of Squid's native ACL language** for all authorization logic — lets every decision be backed by live SQLite lookups (per-user, per-domain, per-path) rather than static config, which is what makes "everything editable from the dashboard, no reconfigure" possible.
- **Three domain modes (`splice`/`bump`/`trusted`) as an explicit privacy/cost tradeoff**, not a single always-decrypt design — most traffic never needs to be decrypted to enforce a domain-level allow/deny, and only domains needing path/show-level rules pay the cost (and trust-warning risk) of SSL-Bump.
- **One shared SQLite database, no caching layer, `ttl=0`** on the `external_acl_type` helpers — every Squid decision is a live read of current dashboard state, so permission changes take effect immediately with no reconfigure/restart.
- **`squid-openssl`, not plain `squid`** — Debian's default `squid` package is built against GnuTLS, which doesn't support SSL-Bump at all; this is an easy, silent trap when rebuilding the Dockerfile from scratch.
- **Crunchyroll show-resolution is deliberately special-cased**, not built as a generic "any site" feature — nothing else in scope has a documented API to resolve "this URL belongs to this show," so generalizing the pattern prematurely would have added complexity with no second user.
- **No rate-limiting/lockout on the dashboard admin login** — a known, accepted gap for the current LAN-only deployment model; see [Security overview](security/overview.md) section 6 before any internet-facing exposure.
- **Squid identity is device-based, not credential-based** (since 2026-08-30) — a client's source IP is resolved through `device_bindings` to a `devices.user_id`, replacing the old per-request Basic-Auth login. This is what makes "no per-device proxy configuration, just a CA cert" possible, at the cost of a different trust boundary (whoever controls a bump-enabled device's IP is treated as its assigned user, with no credential check) — see [Security overview](security/overview.md) section 3 for the full tradeoff.

## Where the v2 redesign discussion lives

The full plan (DNS-tier filtering via a Layer-2 ARP-interception daemon, captive-portal forced enrollment, per-device SSL-Bump curation, YouTube channel filtering, dashboard UI rework, eventual remote access) is tracked in [`RoadMap.md`](../RoadMap.md) at the repo root — kept up to date as phases complete, not just a one-time snapshot. The concrete technical design for Phase 3 specifically (language/library choices, IPC schema, nftables skeleton, DB migration draft) lives in [`docs/design/phase3-technical-design.md`](design/phase3-technical-design.md). As of 2026-08-30 the docs above DO reflect the parts of that redesign that have actually shipped into this repo's proxy/dashboard/database code — Squid's intercept mode and device-based identity, and the devices/groups/identity-model database tables — but Phase 3's actual interception layer (`phase3/arp-worker`, `phase3/nftables-manager`, `controller/`) lives in its own part of the repo tree, is fully built and tested in isolation, and has never been deployed against a real network; RoadMap.md, not this doc, is the authoritative status on that. The one remaining gate before that changes is **G1** — see [`docs/deployment/g1-runbook.md`](deployment/g1-runbook.md) and RoadMap.md's "Path to deployment" section.
