# Roadmap

> Living document. Update this file as items are completed — it's the
> source of truth for "what's next," not chat history or personal notes.
> See [README.md](README.md) for what's built and working today, and
> [`docs/`](docs/project.md) for the technical reference to the current
> system.

## Where this is headed

`parental_proxy` started as a Crunchyroll-only whitelist proxy and is
becoming a full self-hosted replacement for **Bark Home** — whole-home
content filtering, schedules, and reporting for every device on the LAN,
not just ones that can be configured to use an explicit proxy.

Two things distinguish this project's approach from a typical DIY
Pi-hole setup:

1. **Hybrid enforcement tiers.** Most devices/domains get DNS-tier
   filtering (coarse, domain/category-level, works on any device with
   zero per-device setup). A small, deliberately curated set of devices
   get SSL-Bump enabled for path- and show-level rules (Crunchyroll
   today; YouTube channel-level filtering is planned).
2. **No router replacement.** The whole system runs on one existing
   single-NIC box (a Mini PC already running the proxy). It never
   becomes the network's actual gateway/router — it transparently
   intercepts traffic the same way commercial boxes like Bark Home,
   Circle, and Fingbox do, so if it ever crashes, the network is designed
   to keep working unfiltered rather than take the house offline.

---

## Status at a glance

| Phase | What | Status |
|---|---|---|
| 1 | Dashboard modernization (design system, charts, PWA) | ✅ Done |
| 2 | Device/group data model groundwork | ✅ Done |
| — | Filter/picker UI scaling (GH #8) | ✅ Done |
| 3 | Network-level interception (the actual Bark Home replacement mechanism) | 🔶 Milestones 1–9 have real, tested (several functionally verified live) work; not yet deployed anywhere real — see the Milestones list below |
| 4 | Captive-portal forced enrollment | ⬜ Design sketch only, not started |
| 5 | Admin dashboard: responsive layout, installable PWA, control surface | 🔶 Begun — mobile/tablet audit done, one bug fixed |
| 6 | YouTube channel/creator-level filtering | ⬜ Assessed, not started |
| 7 | Remote Access Hardening: TLS, VPN, session/auth model for off-LAN access | ⬜ Not started |

---

## Phase 1 — Dashboard modernization ✅

New CSS design system (cards, responsive, automatic light/dark), a
Report page with a stat strip and Chart.js graphs, a PWA
manifest/service worker, and a restyled kid-facing block page. Same
Flask/Jinja2 architecture throughout — evaluated and explicitly decided
against a React rewrite, since the app has no client state complex
enough to justify one.

## Phase 2 — Device/group data model groundwork ✅

A `devices` table (tracked by MAC address, assignable to a user, a
group, marked "Ignored," or left unassigned) and a `groups` table
(shared-device categories like "TVs," "IoT," "Gaming Computers") with
their own domain allow-lists, mirroring the existing per-user pattern.
Also shipped: per-device domain access grants, device-assignment
cleanup tooling, and instant client-side search on every list page.

**Nothing in the actual proxy enforcement path reads any of this data
yet** — it's admin bookkeeping ahead of Phase 3, the same way
`access_log.approval_requested_at` existed before its UI did.

Along the way: a scalable combobox-style search/select widget replaced
the original chip/checkbox/radio pickers everywhere a user/group/device
needs to be chosen, so the UI doesn't degrade as the number of tracked
entities grows past a handful. That widget replaced an earlier one built
around native `<select multiple>`/checkbox lists — dropped after the
user asked "will holding Ctrl even work?"; the answer was to remove the
need for Ctrl/Cmd entirely (a searchable click-to-add combobox) rather
than explain it, which is why nothing in this app uses a native
multi-select today.

**Key decisions from this phase** (migrated 2026-08-31 from a
since-deleted local handoff doc, `MEMORY.md`, whose narrower prose is
folded in here rather than kept as a second, easily-stale "state of the
project" file alongside this one and `AGENTS.md`):

- **`devices.user_id`/`group_id`/`ignored` are the only source of truth
  for assignment — deliberately no separate `assignment` enum column.**
  An enum + `CHECK` constraint would conflict with the `ON DELETE SET
  NULL` cascades on both foreign keys: deleting a user could leave
  `assignment='user'` with `user_id` now `NULL`, violating a naive
  CHECK. `CHECK (user_id IS NULL OR group_id IS NULL)` stays valid under
  cascades because `SET NULL` only ever makes that OR-condition *more*
  true, never less.
- **Domain access grants use full-replace semantics.** `POST
  /domains/access` deletes every existing grant for that domain and
  re-inserts exactly what was submitted — granting and revoking are the
  same action (check/uncheck a box, then save), not separate endpoints.
- **One composite value encoding is reused everywhere a single
  "assign to X" choice is needed** — device assignment, the Domains
  page's owner filter, etc.: `""` (none/all) / `"ignored"` /
  `"user:{id}"` / `"group:{id}"` / `"device:{id}"`, with one shared
  parsing function rather than a separate boolean/id pair per field.
- **Migration discipline**: while a table/column hasn't been pushed to
  `origin/main` yet, its `CREATE TABLE` can just be rewritten directly.
  Once it's live, further schema changes go through a real `ALTER
  TABLE` inside `common/db.py`'s `_migrate()` instead (see that
  function's own docstring), with a dedicated test that builds the
  pre-migration shape by hand and proves the migration path works
  against it.

---

## Phase 3 — Network-level interception (current focus)

### Goal

Force all real household traffic through the existing Squid/AdGuard
stack at the network level, without requiring per-device proxy
configuration and without turning this box into the LAN's actual
router.

### Fixed constraints (not up for debate)

- Single box, single Gigabit NIC (no second NIC, no hypervisor
  re-platform, no new hardware purchase).
- Must not require replacing or reconfiguring the home router as the
  network's gateway.
- Must not make the later captive-portal phase (Phase 4) "very very
  difficult" or force a rewrite — the interception layer is built now
  with the auth-state hook Phase 4 will need, even though nothing acts
  on it yet.

### Home network (confirmed 2026-08-29)

- Router: Netgear Orbi RBR850 (mesh, with satellites bridging over a
  dedicated backhaul band).
- Modem: Netgear Nighthawk CM2050V (modem only, not the routing
  boundary).
- IPv6: already disabled.
- ARP-spoofing protection (e.g. Dynamic ARP Inspection): **confirmed
  absent** as of the Milestone 1 passive probe (2026-08-29) — captured
  neighbor-table data showed Bark Home's own MAC actively answering for
  both its own IP and the gateway's IP simultaneously, direct evidence
  of it successfully ARP-spoofing this LAN today. Upgrades the earlier
  inference (based only on "Bark Home already works") to an observed
  fact.

### Chosen architecture

Modeled on how Bark Home/Circle/Fingbox actually work: plug into the
same switch/router port as everything else and use **ARP spoofing** — a
Layer-2 MITM technique, legitimate on a network you own — to tell every
device "I'm the router" and the real router "I'm every device." All
traffic flows through this box first, which quietly forwards it to the
real router, which keeps doing 100% of actual routing/NAT/DHCP.

- **`nftables`** transparently redirects intercepted port-53 to a local
  resolver and port-80/443 to Squid; everything else passes through
  untouched.
- **AdGuard Home** sits behind the DNS redirect for the DNS-enforcement
  tier (native per-client policy).
- **Squid** stays exactly as it works today for the bump tier.
- **Fail-open by design, but not for free** — see "Fail-open
  engineering" below. This is a real correction to an earlier
  assumption: a crash does not instantly and passively revert the
  network to normal; recovery has to be actively engineered.

See [`docs/design/phase3-technical-design.md`](docs/design/phase3-technical-design.md)
for the concrete follow-on to this section — language/library choices,
packet-level pseudocode, the IPC message schema, an `nftables` skeleton
for the four policy classes below, systemd unit sketches, and a draft DB
migration. This section stays the "what and why"; that document is the
"with which libraries and roughly what code."

### Daemon architecture (locked in 2026-08-29, after an independent engineering review)

Three separated components, not one monolithic daemon:

1. **A small, project-owned, privileged ARP worker.** Built in **Go**
   (decided 2026-08-29 — see the design doc linked above for why:
   `mdlayher/arp` is a purpose-built RFC 826 implementation, and
   `kubernetes-sigs/knftables`, Apache-2.0 and production-proven inside
   Kubernetes's own network stack, is a materially better fit for the
   nftables-manager than the early-stage/experimental `google/nftables`
   — Go's GC'd, bounds-checked memory model already satisfies "memory
   safe" for this workload without needing Rust's steeper learning
   curve). Holds only `CAP_NET_RAW`. Owns raw ARP transmission, target
   scheduling, gateway/client MAC resolution, and corrective ARP
   restoration on shutdown. One scheduler loop over an immutable
   per-generation target snapshot, not a thread per host.
2. **An unprivileged interception-controller**, fitting the project's
   existing Python stack. Owns desired state, database sync,
   reconciliation, health, and event normalization. Talks to the worker
   over a narrow local IPC boundary (Unix domain socket, peer-credential
   checked) — never a shared process or shared memory space.
3. **A dedicated nftables-manager** (separate `CAP_NET_ADMIN`-scoped
   concern) that updates named sets and does full-table transactional
   reloads. `nftables` natively supports both live named-set updates and
   atomic `nft -f` full reloads, so this doesn't need to be hand-rolled
   for atomicity.

**Bettercap is an optional adapter/fallback, not the production
spoofing engine.** It's actively maintained and has useful discovery
tooling, but its `arp.spoof` module resolves its target list once at
module start — changing targets afterward requires an
off/reconfigure/on cycle, not live mutation — and there's no evidence of
it being used for unattended, multi-month household operation (it's a
pentest/red-team tool). If used at all, it stays stripped down (only
`events.stream`/`api.rest`/`net.recon`/`arp.spoof`, API bound to
localhost) behind a swappable adapter interface, so it's never something
the system actually depends on.

### Authentication and bump-tier: two independent axes (locked 2026-08-30)

Neither flag below controls whether a device is ARP-spoofed — every
in-scope device stays intercepted regardless (that's still governed
purely by `ignored`, per `controller/desired_state.py`). What they
control is what happens to that device's traffic once intercepted, and
they are **two separate, orthogonal decisions**, not one:

**Axis 1 — `devices.is_authenticated`** (the captive-portal gate, Phase
4): every device defaults to gated behind the portal until a person
logs in with their own account, or an admin bypasses/pre-registers it.
Once authenticated, DNS-tier protection (AdGuard) applies — the same
baseline coverage Bark Home provides today, on any device, zero
per-device config. This alone is the ceiling for most devices (the
Smart TV, most kids' primary devices): DNS-tier is *all* they ever get,
by design, not a lesser/temporary state.

- `authenticated_v4` — DNS redirected to AdGuard, normal domain/category
  policy.
- `unauthenticated_v4` — DNS to AdGuard, HTTP redirected to the login
  portal, HTTPS handled by a deliberate pre-auth policy (still open,
  see Phase 4 below).
- `bypass_v4` — infrastructure that must never be touched: Orbi nodes,
  the interception box itself, manually-exempted devices. Same set
  `ignored` devices map to in the ARP-scope decision, per
  `controller/desired_state.py`'s own note.
- `quarantine_v4` — an optional, explicitly operator-triggered isolation
  state.

**Axis 2 — `devices.bump_enabled`** (already existed from Phase 2, now
given a real mechanism): a separate, admin-only, per-device choice —
"this specific device also gets Squid-level refinement" — layered *on
top of* an already-authenticated device, never a substitute for
authentication. This is the mechanism that replaces the household's
current fragile "use Firefox for Crunchyroll, Chrome for everything
else" split with something that works transparently on any app on that
device, no per-app configuration.

- A device with `bump_enabled = 0` (the common case): its port 80/443
  traffic is never touched by this layer at all — DNS-tier is its
  entire filtering story.
- A device with `bump_enabled = 1`: **all** of its port 80/443 traffic
  additionally gets redirected to Squid (nftables can't be selective by
  domain — it can't see hostnames below the TLS layer at all — so this
  redirect is all-or-nothing per device; Squid's own existing SNI-based
  splice/bump decision, unchanged, is what actually narrows this down
  to only the specific domains that need refinement, splicing
  everything else through essentially untouched). The device needs the
  CA certificate trusted once — see the Squid architecture change
  below — no proxy host/port setting anywhere, on any browser or app.

**The hard-deny invariant this session settled on**: a domain marked
`domains.mode = 'bump'` (Crunchyroll today) must never be reachable via
plain unrefined DNS-tier access — it's either properly refined through
Squid, or denied outright with a friendly "ask a parent" page, never a
silent fallback to unfiltered access. Concretely, for a device with
`bump_enabled = 0`, AdGuard itself needs to block `mode='bump'` domains
outright (not just decline to add refinement) — see the AdGuard
integration item in the changes-needed list below, since that
integration doesn't exist in this repo yet.

Nftables consequence: the four sets above stay mutually exclusive
(a device is in exactly one) and continue to drive DNS redirection.
`bump_enabled` needs a **fifth, independent** set (`bump_v4`) that a
device can belong to *simultaneously* with being in `authenticated_v4`
— it is not a fifth mutually-exclusive policy class, it's an add-on
flag. The nftables skeleton and `internal/policy`'s `ResolveConflicts`
(Milestone 5) need correcting for this — see the changes-needed list.

### Squid: explicit-proxy-with-login → transparent intercept (locked 2026-08-30)

**Decision: fully replace, not supplement, today's explicit-proxy +
per-login model for bump-enabled devices.** Today's `proxy/squid.conf.template`
requires a client to be manually pointed at Squid's address (explicit
proxy config) and challenges every request with per-login HTTP Basic
Auth (`proxy_auth`). Neither survives contact with transparent
interception: a NAT-redirected connection has no `CONNECT` handshake,
so there's no way for a client to answer a 407 challenge, and there's
no proxy address to configure in the first place — the whole point is
that no app or browser needs any proxy setting at all.

The replacement is Squid's own **intercept mode** — a standard,
documented feature for exactly this scenario, not something exotic:

- `http_port 3129 intercept` and `https_port 3130 intercept ssl-bump
  ...` replace `http_port 3128 ssl-bump ...`. Squid recovers the real
  destination from the NAT-redirected socket itself
  (`SO_ORIGINAL_DST`) and applies the *same* `ssl_bump peek/splice/bump`
  SNI logic already in the config today — nothing about the per-domain
  decision chain changes, only how the connection arrives.
- **Identity shifts from login to device.** `auth_param basic ...`,
  `acl authenticated proxy_auth REQUIRED`, and `http_access deny
  !authenticated` all go away — there's no login to check anymore. The
  replacement: the client's source IP (`%>a`) resolved through
  `device_bindings` → `devices.user_id` tells Squid which kid this is,
  the same identity data the DNS tier already relies on, just reused
  here instead of a credential prompt. Arguably better UX too — no more
  entering a password into a browser's proxy dialog.
- **The `ssl_bump` catch-all flips from deny to pass-through.** Today's
  `ssl_bump terminate step2 all` is correct only because Squid is
  currently the *sole* filter — nothing else decides "is this domain
  allowed at all." Once AdGuard becomes the authoritative domain-level
  gate (any domain that resolves at all already passed a real check),
  Squid's remaining job narrows to "does this specific domain need
  *extra* refinement" — everything it doesn't recognize should splice
  through by default, not terminate.
- The one thing that does **not** change: the CA certificate still
  needs to be trusted on a bump-enabled device for SSL-Bump to work
  without certificate warnings — same manual step as today, just the
  only one left.

This is a locked architecture decision, not yet implemented — see the
changes-needed checklist immediately below for the concrete work.

### Changes needed to implement this

Schema: no new columns needed — `devices.is_authenticated` and
`devices.bump_enabled` already exist from Phase 2. What's missing is
entirely in the policy-computation and enforcement layers:

- [x] **`common/policy_class.py`** — done 2026-08-30. Added
      `bump_eligible(device_row)` as a second, independent signal
      alongside `PolicyClass`, not folded into the mutually-exclusive
      enum: `bump_eligible` is only ever true when `classify_device()
      == AUTHENTICATED` *and* `bump_enabled = 1` — re-derives
      `classify_device()` itself rather than trusting the flag alone,
      so BYPASS/QUARANTINE/PREAUTH devices can never be bump-eligible
      even if `bump_enabled` was mistakenly set on one. Unit tests in
      `tests/test_policy_class.py` cover all four PolicyClass values.
- [x] **`controller/policy_state.py`** — done 2026-08-30.
      `compute_desired_policy()` now also emits a `"bump"` key (IPs
      where `bump_eligible()` is true), computed independently
      alongside the four `to_set_name()` keys — a device's IP can
      appear in both `"authenticated"` and `"bump"` at once. Covered in
      `tests/test_controller_policy_state.py`.
- [x] **`phase3/nftables-manager/internal/policy`** — done 2026-08-30.
      Added `SetBump`/`policy.DesiredPolicy.Bump []string`, deliberately
      excluded from `AllSetNames` (whose whole contract is mutual
      exclusivity). `ResolveConflicts` now also validates `Bump`
      against the *resolved* `Authenticated` set — a bump IP that isn't
      also authenticated is dropped and recorded as a `Conflict`, never
      trusted blindly. `Reconcile` diffs `Bump` independently. Unit
      tests in `conflict_test.go`/`reconcile_test.go`.
- [x] **`phase3/nftables-manager/internal/nft/knftables_adapter.go`** —
      done 2026-08-30. Removed the blanket `ip saddr @authenticated_v4
      tcp dport 80/443 redirect to :3129/:3130` rules; `authenticated_v4`
      now carries only its DNS redirect. Added the `bump_v4` set (via a
      new `allManagedSets` list, since it's outside `AllSetNames`) and
      its own independent `ip saddr @bump_v4 tcp dport 80/443 redirect
      to :3129/:3130` rules, so it composes with (not instead of)
      `authenticated_v4`'s DNS rules. `EnsureBaseline`/`ReadActual` both
      updated to manage all five sets. **Verified for real** on the
      smoke-test VM: `go build`, `go vet`, `gofmt -l`, and `go test
      -count=5` (including a new end-to-end case in
      `TestEnsureBaselineThenApplyDiffs_AgainstFake` proving an IP lands
      in both `authenticated_v4` and `bump_v4` simultaneously against
      knftables' real in-memory `Fake`) all clean — this is Go logic
      proven against a real build, not written from memory and left
      unverified. **Further verified against a real kernel 2026-08-30**
      (see the new live-verification section below): `EnsureBaseline`
      called twice in a row against the smoke-test VM's actual nftables
      produced exactly the intended 6 redirect rules both times (not
      12), confirming both the `bump_v4` rule syntax and the
      flush-before-re-add idempotency hold outside `Fake` too. Also
      found and fixed a real bug in this same pass, in a file this
      checklist item didn't originally call out:
      `internal/dbsource/sqlite.go`'s `desiredPolicyWire` never declared
      a `"bump"` JSON field, so `ReadDesiredPolicy` silently discarded
      every bump IP `controller/policy_state.py` had actually computed
      — `pp-nftables-manager` would never have redirected a single
      device to Squid in production despite the DNS-tier sets working
      correctly. Fixed with a regression test
      (`internal/dbsource/sqlite_test.go`, new file — this package had
      no tests at all before).

- [x] **`proxy/squid.conf.template`** — done 2026-08-30. Replaced the
      explicit `http_port 3128 ssl-bump` + `proxy_auth` block with
      `http_port 3129 intercept` / `https_port 3130 intercept
      ssl-bump ...`; removed the `auth_param`/`acl authenticated`/
      `http_access deny !authenticated` lines entirely. **Deliberately
      did NOT flip the `ssl_bump terminate step2 all` catch-all (or the
      final `http_access deny all`) to allow/splice**, despite this
      checklist entry originally listing that flip — the entry's own
      caveat ("needs a closer look... not assumed here") turned out to
      matter: `sni_show_block_page`'s ERR case (i.e.
      `block_page_mode = 'terminate'`, the default) falls through to
      exactly this catch-all, so flipping it now — before the AdGuard
      hard-deny integration below actually exists to be the domain-level
      gate — would silently splice unconfigured/unassigned domains
      through unfiltered instead of denying them. A real regression, not
      a no-op. Both catch-alls stay deny-by-default until the AdGuard
      item ships; guarded by two new regression tests
      (`test_ssl_bump_catchall_is_still_terminate_not_splice`,
      `test_http_access_catchall_is_still_deny_not_allow`) so this isn't
      silently re-flipped later without the AdGuard piece actually being
      in place. `proxy/basic_auth_helper.py` (now orphaned — nothing in
      intercept mode calls `auth_param basic`) was removed, along with
      its Dockerfile `COPY` line and its dedicated tests.
- [x] **`proxy/sni_helper.py` / `proxy/authz_helper.py`** — done
      2026-08-30. Both dropped their `login` parameter entirely; identity
      is now resolved via a new shared `common/device_identity.py`
      (source IP → `device_bindings` → `devices.user_id` → `users` row),
      used by both. `external_acl_type` FORMAT strings in
      `squid.conf.template` updated to match (no more `%LOGIN`, field
      counts down by one each). Full local pytest suite green (339
      passed) after updating `tests/test_helpers_protocol.py` and
      `tests/test_squid_conf_regressions.py` to match. **Booted against
      a real Squid binary for the first time 2026-08-30** (see the new
      live-verification section below) — found and fixed three real
      bugs no Python unit test could have caught, all now live and
      staying up: (1) `docker-compose.yml`'s `proxy` service needed
      `cap_add: NET_ADMIN` for `intercept`'s `IP_TRANSPARENT`/
      `SO_ORIGINAL_DST` use; (2) that alone wasn't enough, because this
      Squid build drops root privileges internally
      (`--with-default-user=proxy`) before opening the intercept
      listeners, and Linux clears capabilities across that internal
      `setuid()` — fixed with `setcap cap_net_admin=+ep` on the squid
      binary itself in `proxy/Dockerfile`, which survives it; (3) an
      intercept-only Squid (no plain forward-proxy `http_port` at all)
      FATALs at startup trying to build its own internal icon URLs
      (`mimeLoadIcon: cannot parse internal URL`, visible only in
      `/var/log/squid/cache.log`, not stdout) — fixed by adding a
      loopback-only, non-`intercept` `http_port 127.0.0.1:3128` purely
      so that URL construction has somewhere valid to point at.
- [x] **AdGuard Home integration** — done 2026-08-30, and the hard-deny
      invariant above is now real, not just designed for. `adguard/`
      wraps the official `adguard/adguardhome:v0.107.79` image with an
      automated first-run bootstrap (`entrypoint.sh`, via AdGuard's own
      `/control/install/configure` API — no manual wizard);
      `common/adguard_client.py` is a thin stdlib-only REST client;
      `controller/adguard_sync.py` builds one AdGuard regex rule per
      `mode = 'bump'` domain, scoped via the `$client=ip1,ip2` modifier
      to every currently non-`bump_enabled` device's active IP, and
      pushes it as a full-replace via `/control/filtering/set_rules` --
      same idempotent-full-reconcile shape as everywhere else in this
      codebase. Wired into `controller/main.py` as a third periodic task
      alongside the heartbeat pacer and discovery loop
      (`--adguard-url`/`--adguard-username`/`--adguard-password`/
      `--adguard-interval`).

      **Verified live end-to-end 2026-08-30**, not just unit-tested:
      real `docker compose` stack (proxy + adguard + dashboard), a real
      bump-mode domain, two real client containers with real
      `device_bindings` rows (one `bump_enabled=1`, one `bump_enabled=0`)
      — `dig`ging that domain from the non-bump client returned `0.0.0.0`
      (hard-denied), the identical query from the bump-enabled client
      resolved normally (so Squid can still refine it), and an unrelated
      domain resolved fine from the non-bump client too (the deny is
      scoped, not a blanket block). Also confirmed the merge logic for
      real: a hand-added "admin's own" AdGuard rule survived untouched
      across a sync cycle that replaced a stale managed block sitting
      right next to it.

      Three real bugs found and fixed while first booting this against
      a real instance (same category as the Squid pass immediately
      before this one — see the live-verification section below):
      (1) `/install/configure` and `/install/get_addresses` are NOT the
      real paths despite what AdGuard's own generated OpenAPI-doc
      tooling implies — every route lives under `/control`, even before
      the instance is configured at all; (2) requesting AdGuard's admin
      UI bind directly onto `127.0.0.1` (this project's own secure
      default, mirroring `DASHBOARD_BIND`) self-conflicted with
      `install/configure`'s own bind-validation check against its still-
      running pre-configure listener on that exact address — fixed by
      always configuring onto the wildcard address first, then rewriting
      `AdGuardHome.yaml`'s `http.address` directly and restarting onto
      it if a non-wildcard bind was actually requested.

      **Both remaining items closed 2026-08-30**: `controller/`,
      `phase3/nftables-manager/`, and `phase3/arp-worker/` all have
      Dockerfiles now and are wired into `docker-compose.yml` as real
      services (see the Milestones list's own updated status below for
      the full writeup and live verification); and
      `dashboard/block_page_server.py` gives plain-HTTP hard-deny
      requests a real friendly page via AdGuard's `$dnsrewrite` modifier
      (HTTPS deliberately excluded — see that module's own docstring;
      the live-verification section below has the full writeup).
- [x] **Captive portal (Phase 4) — begun 2026-08-31, every design-sketch
      bullet done and verified the same day** (user: "let's begin Phase
      4"): gate any newly-seen MAC not already registered as
      bypass/ignore (Milestone 1); a kid-facing login that grants
      `is_authenticated` (DNS-tier) only, never `bump_enabled`
      (Milestone 3); an admin-facing quick-add path at first sight of a
      new device (add to bypass, assign to a group -- Milestone 2's
      Bypass/Manage actions, satisfying the sketch's own "or full
      dashboard access from another device" alternative); and a
      reminder screen for an account that's meant to have both DNS and
      Squid but hasn't had the one-time CA cert install done yet (both
      directions -- see the dated entry below). The recommendation this
      bullet originally flagged as "not yet confirmed" (admin should
      only flip `bump_enabled` after confirming the CA cert is actually
      installed) is now built as a real `confirm()` prompt, not just a
      recommendation. See the dated milestone entries under "Phase 4"
      below for the full build/verification trail of each piece.

### The core architectural claim, verified live end-to-end (2026-08-30)

Everything above — ARP-spoof a victim, transparently redirect its
traffic via `nftables`, driven by DB policy — was proven together for
real, on a Docker bridge network standing in for a real LAN switch (see
the ARP-worker README for why this, not a cloud VM, is the right free
substitute). Real processes throughout: `pp-arp-worker`,
`pp-nftables-manager`, `controller/main.py`, a real SQLite DB, real
`curl` traffic.

Sequence: a victim container with no route to the interception box at
all first confirmed to get nothing on the "gateway"'s port 80
(baseline — nothing was listening there). Then, with the ARP worker
actively poisoning the victim's cache and the nftables-manager applying
the victim's `authenticated_v4` membership computed from a real DB row,
the victim's `curl http://<gateway-ip>/` request — addressed to what it
still believes is the real gateway — was transparently delivered to a
local HTTP listener standing in for Squid, returning that listener's
distinct content. Then, **without stopping or restarting anything** —
the ARP worker kept poisoning, the controller kept its connection,
nftables-manager kept its reconcile loop running — the device's
`is_authenticated` flag was flipped to 0 in the DB. On its next poll
cycle, nftables-manager moved the victim's IP from `authenticated_v4`
to `unauthenticated_v4`, and the *same* `curl` request from the *same*
still-poisoned victim immediately started landing on a second local
listener standing in for the future login portal instead — proving
policy reclassification takes effect live, independent of the ARP
interception layer, exactly matching the "interception scope and
policy scope are different axes" design decision.

This is the strongest verification available without a real LAN.
What's still unverified: the two gaps this note originally called out
(real Squid, real AdGuard behind these redirects) are both closed
below now. What remains is a real switch's more complex behavior (STP,
VLANs, actual physical NICs) instead of a Linux bridge, and everything
the Orbi validation section below calls out (mesh roaming, wireless
backhaul, satellite-attached clients).

### Squid intercept mode + bump_v4, verified live end-to-end (2026-08-30)

Before this pass, every piece of the intercept-mode rewrite (Squid
config, `device_identity.py`, `bump_v4`'s nftables rules) had only ever
been exercised by Python/Go unit tests against mocked behavior — it had
never once been booted for real, the same gap the original v1 proxy
work had before its own live Squid pass turned up four real bugs (see
`docs/review-2026-08-28.md`). This pass closed that gap the same way:
real `pp-nftables-manager` binary against the smoke-test VM's real
kernel, real Squid in intercept mode, real client containers, real
external HTTPS traffic (`example.com`), nothing mocked.

**Topology note, since this matters for what the result proves**: the
first attempt ran Squid as an ordinary Docker Compose service (its own
bridge-network namespace) with the `bump_v4` NAT-redirect rules
installed in the *host's* namespace — this is a Docker-testing
artifact with no equivalent in production (a physical box only has one
namespace), and it broke `SO_ORIGINAL_DST` recovery: the pre-NAT
destination conntrack records is per-namespace, so a redirect applied
in one namespace doesn't carry into a socket listening in another.
Re-running Squid with `--network host` (so nftables and Squid share
exactly one namespace, matching the real single-box deployment target)
fixed it immediately. Worth remembering for any future Docker-based
test of this specific pair — real deployment doesn't have this
problem, Docker's default per-container networking does.

With that corrected, the full pipeline was proven live:
- `pp-nftables-manager`'s `EnsureBaseline` against a real kernel, output
  inspected directly via `nft list table inet parental_proxy` — matched
  the intended ruleset exactly, including the two `bump_v4` lines, and
  stayed at 6 redirect rules (not 12) after being called twice.
- A client container's IP added to the real `bump_v4` set redirected
  its own outbound 80/443 into Squid's intercept ports, transparently
  — no proxy configuration on the client at all.
- Squid recovered the true pre-NAT destination via `SO_ORIGINAL_DST`
  (`ORIGINAL_DST/<real-ip>` in `access.log`, not Squid's own address) —
  bumped it, and served the real page (`TCP_MISS/200`).
- Identity resolution off the client's source IP alone (no `%LOGIN`)
  worked both ways: a device with a real `device_bindings` row
  resolved to its user and was allowed by a `bump`+`is_global` domain;
  a second device in `bump_v4` with **no** binding at all was correctly
  denied (403) by the exact same domain — confirming
  `device_identity.resolve_user()`'s "no identity" fallback actually
  denies in practice, not just in its unit tests.
- An unconfigured domain (`wikipedia.org`, never added to `domains`)
  hit the `ssl_bump terminate step2 all` catch-all and the connection
  was cleanly terminated (curl: `HTTP_CODE=000`) — confirming the
  deliberate deny-by-default catch-all discussed in the checklist above
  still holds against real traffic, not just in config-parsing tests.

Three real bugs were found and fixed along the way (`cap_add:
NET_ADMIN`, `setcap` on the squid binary, the loopback `http_port` for
internal icon URLs — see the checklist item above for detail) — none
of them were reachable by any existing test, Python or Go, because
none of them exercise a real container boot. All test/seed artifacts
(client containers, the nftables table, DB rows) were torn down
afterward; the VM was left at a clean `docker compose down -v` state,
matching how this pass found it.

### AdGuard Home hard-deny, verified live end-to-end (2026-08-30)

Immediately following the Squid pass above, closed the last item on
this checklist the same way: real `docker compose` stack (`proxy` +
the new `adguard` service + `dashboard`), real AdGuard Home
`v0.107.79`, real client containers, real DNS queries -- nothing
mocked. See the checklist item above for the three real bugs found
getting AdGuard to boot automated at all; this section is the actual
end-to-end proof once it was up.

Two client containers on the compose network, two real `devices`/
`device_bindings` rows (one `bump_enabled=1`, one `bump_enabled=0`) and
one bump-mode domain (`example.com`, plus the always-seeded
`crunchyroll.com`). Ran the real `controller/adguard_sync.sync_once()`
against the real shared DB and the real running AdGuard instance --
confirmed it pushed exactly the two expected rules
(`/(?i)(?:^|\.)(?:crunchyroll\.com)$/$client=<non-bump-ip>` and the same
for `example.com`), each scoped to only the non-bump device's IP. Then
the actual test: `dig`ging `example.com` from the non-bump client
returned `0.0.0.0` (hard-denied, exactly the invariant this whole item
exists for); the identical query from the bump-enabled client resolved
to real IPs (so Squid still gets a chance to refine it); a third,
unrelated domain (`wikipedia.org`) resolved fine from the *non-bump*
client too, confirming the deny is scoped to bump-mode domains
specifically, not a blanket block for that device.

Also verified the merge logic that keeps this from ever touching an
admin's own AdGuard configuration: manually pushed a fake "admin rule"
(`||some-admin-added-rule.example^`) sitting right next to a stale,
already-bracketed managed block, ran `sync_once()` again, and confirmed
the admin rule survived byte-for-byte in its original position while
the stale block was replaced with the fresh, correct one.

All test containers, DB rows, and the AdGuard/proxy/dashboard volumes
were torn down afterward (`docker compose down -v`); the VM was left at
a clean state, and the full pytest suite re-confirmed 389 passed, 0
skipped.

### Network-wide ad blocking via curated uBlockOrigin/uAssets lists (2026-08-30)

Added immediately after the hard-deny work above, at the user's
request ("pull in the list of assets from uBlockOrigin"). Corrected a
wrong assumption along the way, worth remembering: **AdGuard Home's own
`/control/install/configure` already registers and enables "AdGuard DNS
filter" automatically**, with real rules populated within seconds of
configuring (confirmed live: 179,158 rules) — checking the raw
`AdGuardHome.yaml` file too early (as an earlier point in this same
session did) makes it look like zero filters are active, which isn't
true once the live `/control/filtering/status` API is checked instead.
This isn't filling an empty void, then — it's a genuine complementary
layer on an already-functioning baseline, and the user's own follow-up
question ("or does AdBlock natively perform this function already")
was the right question to ask.

uBlock Origin's own lists are written for a browser extension (cosmetic
element-hiding, JS scriptlet injection) that a DNS server fundamentally
cannot apply — pulling in the whole `uAssets` repo blindly would mostly
be wasted bytes. Instead of guessing, each candidate list was actually
subscribed to on a throwaway AdGuard instance and its resulting
`rules_count` (the DOMAIN-blocking subset AdGuard's DNS engine can
actually use) checked before committing to it: `filters.txt` (uBO's
main list, 6,076 usable rules despite being mostly cosmetic overall),
`badware.txt` (4,290), `privacy.txt` (1,743), `resource-abuse.txt`
(77), and `unbreak.txt` (2,543 — the matching exceptions list for the
other four, included specifically to counteract their false
positives). Explicitly left out: `annoyances*.txt` (cookie-banner/
cosmetic-heavy, real over-blocking risk for low DNS-blocking value),
`experimental.txt` (opt-in even within uBO itself), the per-year
`filters-20XX.txt` archives, and `ubol-filters.txt`/`lan-block.txt`/
`ubo-link-shorteners.txt` (niche, not obviously a sane household
default).

Wired into `adguard/entrypoint.sh`'s existing first-run bootstrap (right
after `/control/install/configure` succeeds) via
`/control/filtering/add_url` for each list — `ADGUARD_SKIP_EXTRA_BLOCKLISTS=1`
opts out and keeps only AdGuard's own default filter. One more small
real finding: this Alpine-based image's busybox `wget` has no
`--user`/`--password` flags at all, so the `Authorization: Basic` header
for these (post-configuration, login-protected) calls has to be built
by hand -- using busybox's own `base64` applet, confirmed present in
this image.

**Verified live end-to-end** through the real `docker compose` bootstrap
flow (not just the throwaway probe used to pick the lists): all 5 lists
plus AdGuard's own default filter came up enabled with the same rule
counts as the probe, and real ad/tracker domains
(`doubleclick.net`, `pagead2.googlesyndication.com`) resolved to
`0.0.0.0` from a client container on the compose network. Full pytest
suite re-confirmed clean (389 passed) afterward; VM left at a clean
`docker compose down -v` state.

### Filter-list update checking: weekly auto-refresh + an admin "check now" button (2026-08-30)

User asked for a way to keep the ad-block lists above current -- a
periodic check (weekly) plus an admin-triggerable manual check. Used
AdGuard Home's own built-in mechanisms rather than reimplementing
update-checking in this project's own code, since it already has a
mature one: `adguard/entrypoint.sh` now also calls
`/control/filtering/config` once during first-run bootstrap
(`interval=168`, confirmed live to be accepted and echoed back exactly
by `/control/filtering/status`, matching AdGuard's own "Once a week" UI
preset) -- this is AdGuard's own background schedule, not something
worth duplicating. `common/adguard_client.py` gained
`set_filters_update_interval()` (used by the above) and
`refresh_filters()` (POST `/control/filtering/refresh`, confirmed live
safe to call as often as wanted, per AdGuard's own docs) for the
"whenever the admin wants" half.

The dashboard's Settings page gained a new card: a "Check for filter
updates now" button, plus an editable AdGuard connection-settings form
(URL/username/password) bootstrapped from the same `ADGUARD_*` env vars
the `adguard` container itself uses -- editable afterward exactly like
the dashboard's own admin login, specifically so an operator who left
`ADGUARD_PASSWORD` blank (auto-generated, printed only to the `adguard`
container's own logs) can paste it in by hand once and have it work
from then on.

**Found and fixed a real networking conflict working this out**:
AdGuard's admin UI defaults to `127.0.0.1`-only (`ADGUARD_WEB_BIND`, a
deliberate secure default from the pass immediately before this one) --
a loopback-bound socket only accepts connections from within the exact
same network namespace, so the (until now) bridge-networked `dashboard`
container could never have reached it at all, not even via
`host.docker.internal`/`extra_hosts` (that arrives through a
bridge-facing address, a genuinely different source than `127.0.0.1` as
far as a strict loopback bind is concerned). Fixed by giving `dashboard`
`network_mode: host` too, matching `proxy`/`adguard` -- its own
`DASHBOARD_HOST` now takes over what the old port-mapping's bind
address used to control, the same "app's own listen address gates LAN
exposure under host networking" pattern already established for
`ADGUARD_WEB_BIND`. Worth remembering: every time a new inter-service
call gets added to this stack, host networking's reachability rules
need rechecking -- they're not the same as bridge networking's.

**Verified live end-to-end** through the real `docker compose` flow:
confirmed the dashboard (host-networked, listening on `127.0.0.1:8787`)
successfully calls AdGuard's real refresh API over `127.0.0.1:3000` and
gets back "Checked now -- everything was already up to date." (the
correct, healthy result immediately after a fresh bootstrap that just
fetched everything); confirmed `/control/filtering/status` reports
`interval: 168` and all filter lists populated with their expected rule
counts. Full pytest suite re-confirmed clean (400 passed) afterward; VM
left at a clean `docker compose down -v` state.

### Full interception stack containerized and verified live end-to-end (2026-08-30)

Picking up the "what are next steps" discussion from the previous
session, moved forward on all three recommendations without further
check-ins, as asked, surfacing decisions only where one genuinely had
to be made (none did, this time). This is the single biggest structural
change of the day: `phase3/arp-worker/`, `phase3/nftables-manager/`, and
`controller/` all gained real Dockerfiles and real `docker-compose.yml`
service definitions, gated behind a `profiles: ["interception"]` compose
profile so plain `docker compose up` stays exactly as it was (proxy +
adguard + dashboard only) -- starting the profile for real is a
separate, explicit, deliberately un-defaulted decision
(`ARP_WORKER_IFACE`/`GATEWAY_IP`/`GATEWAY_MAC` have no sensible
defaults; each binary's own existing argument validation refuses to
start without them, rather than a compose-level hard requirement that
would break the default profile's own parseability). `controller/`
gained a `requirements.txt` (its first, `pyroute2` for the rtnetlink
listener above) and its `sys.path` bootstrap was updated to handle both
a flat Docker layout and a real repo checkout.

**Verified live end-to-end** via the same safe Docker-bridge pattern
used throughout this project (a disposable, isolated test network --
never the real production Beelink or an actual household LAN): all six
services (`proxy`, `adguard`, `dashboard`, `arp-worker`,
`nftables-manager`, `controller`) came up together via
`docker compose --profile interception up -d`; seeding one real device
into the shared DB was picked up by the controller, sent to the real
`arp-worker` binary over their shared Unix socket, and independently
computed into the real kernel's `authenticated_v4` nftables set by
`nftables-manager` -- confirmed by reading the real ruleset directly
(`nft list table inet parental_proxy`), not just trusting log output.
The victim container's own ARP cache was confirmed genuinely poisoned
(pointing the gateway's IP at the interception box's own bridge-
interface MAC, not the real gateway's), and confirmed genuinely
restored to the real gateway's MAC the moment
`docker compose --profile interception stop` sent SIGTERM -- the exact
fail-open guarantee this whole architecture exists to provide, proven
for the first time through a real container lifecycle rather than a
bare Go process.

**Three more real bugs found and fixed along the way**, all in code
that pre-dates this session but had simply never been exercised as an
actual running deployment before:
1. `internal/worker/worker.go`'s `sendGratuitousReply` had discarded
   every `ARPSender.Reply()` error since it was written (`_ = err`,
   with its own TODO comment saying so) -- the controller reported
   "generation applied," `nftables-manager` correctly updated
   `authenticated_v4`, and the victim's ARP cache never changed at all,
   with nothing anywhere logging a single failure. Added
   `Config.OnSendError` (optional, nil-safe) so `main.go` can actually
   log send failures -- turned out the underlying sends were fine once
   this was in place (the earlier silent failure was itself the actual
   diagnostic obstacle, not a symptom of a second bug), but the
   observability gap was real and is now closed regardless.
2. A dead worker connection was previously only ever noticed if
   desired state happened to change across the outage: `reconcile()`
   correctly returns `None` when nothing has changed, so `run_cycle()`
   never touches the connection at all once a generation is applied --
   meaning the heartbeat pacer's own repeated failures, only ever
   logged and never acted on, were the sole signal available, and
   nothing was listening to them. Fixed with a `threading.Event` the
   heartbeat's error callback sets specifically for
   `WorkerConnectionError`, checked at the top of the main loop and
   routed through the exact same reconnect path `run_cycle()`'s own
   failures already used.
3. `controller/Dockerfile`'s `python:3.12-slim` base doesn't ship
   `iproute2` -- `discovery.py`'s snapshot loop failed every single
   cycle with `[Errno 2] No such file or directory: 'ip'`, silently
   (logged as a warning, retried forever, never crashed the container).
   Fixed by installing `iproute2` in the image.

Full pytest suite re-confirmed clean at every step (419, then 435 passed
on the VM as new tests were added); all test containers, networks,
volumes, and the one-off `.env` file used for this pass were torn down
afterward, VM left at a clean `docker compose down -v` state matching
how it was found.

### Friendly landing page for AdGuard-blocked domains, HTTP only (2026-08-30)

The third recommendation, tackled last. `dashboard/block_page_server.py`
is a tiny stdlib-only HTTP server (no Flask) that
`dashboard/dashboard.py`'s `main()` starts on port 80, only when
`DASHBOARD_URL` is set -- the exact same gating condition
`proxy/entrypoint.sh` already uses for Squid's own `deny_info` line, and
the exact same env var, reused rather than duplicated.
`controller/adguard_sync.py`'s hard-deny rules can now carry AdGuard's
`$dnsrewrite` modifier alongside `$client`, pointing a blocked domain's
DNS answer at the dashboard's LAN IP -- confirmed live combinable with
`$client` on one rule (a shell-escaping artifact in this session's own
testing briefly looked exactly like an AdGuard parser bug -- `$client`
and the modifier name itself were vanishing from the echoed rule text
-- until a clean script-file invocation, no shell involved, proved the
feature works exactly as documented and the corruption was entirely on
this session's own testing side).

**Deliberately HTTP-only, a design constraint stated plainly rather than
worked around**: there is no HTTPS equivalent and there will not be one
here. Terminating TLS for an arbitrary blocked domain needs either that
domain's real certificate or a device that already trusts this
project's own SSL-Bump CA -- and non-bump devices are, by the entire
point of the "two independent axes" design, never asked to trust it.
Showing a "your connection is not private" warning on every hard-denied
HTTPS domain would be strictly worse than today's plain connection
failure, by this project's own already-established reasoning
(`dashboard.py`'s `SETTINGS_BODY` defaults Squid's equivalent choice,
`block_page_mode`, to "just fail the connection" for exactly this
reason). Confirmed live that the port-80-only design doesn't regress
the HTTPS case either: the dashboard has no TLS listener anywhere, so a
redirected HTTPS attempt gets a clean `Connection refused` -- no worse
than the pre-existing `0.0.0.0` behavior, just arriving at a real IP
instead of a null one.

**Found and fixed one more real bug of the exact same shape as the
Squid `CAP_NET_ADMIN` fix from an earlier pass**: `docker-compose.yml`'s
`cap_add: NET_BIND_SERVICE` alone wasn't enough for the dashboard's
non-root `proxy` user to actually bind port 80 (`PermissionError:
[Errno 13] Permission denied`, confirmed live) -- a container-level
capability only reaches a non-root `execve()` if the exec'd binary
itself also carries a matching file capability. Fixed with
`setcap cap_net_bind_service=+ep` on the real `python3.12` binary in
`dashboard/Dockerfile`, resolved past the `python3` symlink, mirroring
`proxy/Dockerfile`'s own `setcap` fix for Squid exactly.

**Verified live end-to-end**: seeded a real hard-deny domain and a real
non-bump device, ran the real `adguard_sync.sync_once()` with a real
`block_page_ip`, and confirmed from a real client container that the
domain resolved to the dashboard's IP and that an HTTP request with
that `Host` header got back the real friendly page naming the specific
blocked domain -- and separately confirmed the HTTPS case's clean
`Connection refused`, per the paragraph above. 23 new tests. Full
pytest suite: 435 passed on the VM. All test containers/networks/the
`.env` file torn down afterward.

### Dashboard "interception health" view, plus real bugs found running the full stack live for the first time (2026-08-30)

Built the dashboard `/health` view the Milestones summary above had
flagged as unbuilt (`dashboard/dashboard.py`'s `health_page()`): shows
`interception_runtime`'s `mode`/`last_healthy_at`/`fail_open_reason`
(controller<->arp-worker pipeline) and `nft_mode`/`nft_last_healthy_at`/
`nft_fail_reason` (nftables-manager) as green/red/amber badges, plus a
sidebar "!" alarm badge visible from every page. Live-verified against
the real containerized stack on the smoke-test VM.

**Staleness detection, added after a real live finding**: OOM-killing
the controller container (`docker update --memory 10m`, sustained) put
it into a genuine SIGKILL/restart crash loop, but
`interception_runtime.mode` stayed frozen at `'running'` forever --
the code path that would write `fail_open` lives in the same process
that keeps dying before it can run. `_is_stale()` now flags either
column "stale" (amber, explicit explanation) when its `last_healthy_at`
hasn't advanced in over 30s, independent of what `mode` still says.

**A real bug found and fixed along the way**: `common/adguard_client.py`'s
`get_custom_rules()` raised on AdGuard's own real response shape -- a
freshly-configured instance that's never had a custom rule set reports
`user_rules: null`, not `[]` -- which meant AdGuard domain-block-rule
sync could never complete a single cycle on any brand-new deployment,
forever. Fixed and confirmed live against a brand-new AdGuard instance.

**A `/code-review max` pass the same day caught a real regression in
that same fix** before it shipped further: the first version of the
null-handling fix was too broad -- it also silently swallowed a
non-dict or key-missing response as "no rules yet," which would have
let `sync_once()`'s full-replace write silently erase an admin's real
custom rules on a merely malformed read, not just the one confirmed
benign case. Tightened to only special-case an explicit `null` with the
key present; a missing key or non-object response still raises and
fails closed, as before. The review also caught a test
(`test_health_page_shows_running_mode_and_generation`) whose hardcoded
absolute timestamp had already aged past the staleness threshold by
the time of review, silently testing the wrong render branch -- fixed
to use a relative, always-fresh timestamp. Two known-but-deferred
design gaps from that same review, not fixed yet:
- Staleness detection lives only in the dashboard's read path,
  computed transiently per page load, and is never written back to
  `interception_runtime` -- any other future consumer of that table
  (an API, a CLI tool, alerting) still sees a stale `mode` forever.
  Fixing this properly likely means the peer process (or a lightweight
  watchdog) writing `fail_open` on a dead process's behalf, not another
  dashboard-side heuristic.
- `HEALTH_STALE_AFTER_SECONDS = 30` is hardcoded in `dashboard.py`,
  disconnected from `controller`'s `--poll-interval` and
  `nftables-manager`'s `-poll-interval` (both default 5s today, but
  nftables-manager's own README calls its default "a guessed default,
  not soak-tested"). Should probably be derived from a poll interval
  each writer persists into the DB itself, not assumed.

A third finding from the same review WAS fixed the same evening (this
one was mechanical, not a design judgment call like the two above):
`render()` and `health_page()` were independently querying the same
`interception_runtime` singleton row and independently re-deriving "is
this subsystem unhealthy" in two different shapes that only agreed by
De Morgan coincidence. Both now share one query (`_get_runtime_row()`)
and one predicate (`_subsystem_unhealthy()`/`_subsystem_stale()`).

**Milestone 9 fault-campaign progress, container-testable slice**: also
used this session's containerization work (not available before it) to
run two real fault-injection tests against the smoke-test VM's live
stack: an ungraceful crash of `arp-worker` (found that `docker kill`
does NOT trigger Docker's `restart: unless-stopped` -- only a crash
from inside the container's own PID 1 does; confirmed the controller's
reconnect logic recovers correctly either way) and the OOM-kill test
above. NIC down/up and gateway reboot still need real hardware.

- [x] **TODO, next session the smoke-test VM is back online** — ordered
  plan, written up 2026-08-30 end-of-night specifically so this can run
  with minimal check-ins. Each step is self-contained; do them in order,
  fix anything genuinely broken the same way this session did (don't
  just report and stop), and write up what happened in this section (or
  its own dated section below, matching this session's pattern) as you
  go rather than batching it to the end. Stop and check in only for the
  two flagged exceptions at the bottom — everything else is a "just go
  do it" task. **Done 2026-08-31 — see dated writeup below.**

  1. **Housekeeping.** `git pull --ff-only` (expect a fast-forward onto
     today's commits, nothing local should be ahead). Check whether the
     controller's Docker memory limit from last night's OOM fault test
     survived (`docker inspect parental-proxy-controller --format
     '{{json .HostConfig.Memory}}'`) — if it's still the crippling test
     value, `docker update --memory 0 parental-proxy-controller` to
     clear it before anything else.
  2. **Go test verification** (the one thing this session couldn't do at
     all — no Go toolchain in the dev sandbox). From
     `phase3/nftables-manager/`: `go build ./...`, `go vet ./...`, then
     `go test ./...` — specifically confirm
     `TestWriteHealth_DoesNotAdvanceLastHealthyOnFailOpen` and
     `TestWriteHealth_FailOpenOnFirstWriteLeavesLastHealthyNull` (new
     this session, commit `d53a8a8`) pass. `-race` isn't available
     (no C compiler) — use `-count=10` for flake-checking if anything
     seems marginal, matching this project's established substitute. Do
     the same build/vet/test pass for `phase3/arp-worker/` too, as a
     baseline confirmation nothing else regressed.
  3. **Full Linux pytest run.** `pytest -v` from the repo root — this is
     the first time the full 447-test suite (up from 435) runs on
     Linux since this session's changes landed; Windows only ran 425 of
     them (22 are `AF_UNIX`-only and skip there). Should be 447 passed,
     0 skipped.
  4. **Live redeploy + smoke test of the whole stack**, all six
     containers together for the first time since this session's
     changes: `docker compose --profile interception up -d --build`.
     Confirm all six containers come up and stay up, hit `/health` in
     the browser and confirm both subsystems show green "running" (not
     stale, not fail-open), and check `docker compose logs controller`
     for zero AdGuard-sync warnings (confirms the tightened
     `get_custom_rules()` fix still completes real sync cycles against
     a real AdGuard instance, not just its unit tests).
  5. **Fault-test `nftables-manager` specifically** — the one component
     NOT fault-tested last session (only `arp-worker` and `controller`
     were), and the one most worth proving live given the `WriteHealth`
     fix now getting real Go-test coverage in step 2. Two scenarios,
     same techniques as last session:
     - Ungraceful crash: `docker exec parental-proxy-nftables-manager
       sh -c 'kill -9 1'` (NOT `docker kill` from outside — that
       bypasses `restart: unless-stopped`, see last session's own
       finding). Confirm it auto-restarts and `/health`'s nftables card
       recovers to "running" on its own.
     - Sustained OOM-kill: `docker update --memory 10m
       parental-proxy-nftables-manager`, confirm `OOMKilled=true` and a
       crash loop, then confirm `/health` correctly flags the nftables
       card "stale" (not a false "running") within
       `HEALTH_STALE_AFTER_SECONDS` (30s) — this is the exact scenario
       the `WriteHealth` fix targets, so this is real end-to-end proof
       beyond the Go unit tests. Reset the memory limit
       (`docker update --memory 0 ...`) afterward.
  6. **Tear down test state**: any throwaway Docker networks created
     for this (e.g. `ppfaulttest`-style), reset any memory limits still
     set on any container, and leave `.env` in a normal, non-fault-test
     state — matching this project's own "leave the VM clean" discipline
     from every prior live-testing round.

  **Stop and check in, don't guess, if either of these comes up:**
  - The VM itself is in a broken/inconsistent state (disk full, a
    snapshot didn't restore cleanly, git history diverged) — that needs
    you to actually intervene (restore the snapshot), not a code fix.
  - Everything above finishes clean with time to spare. The next real
    items on the roadmap (Phase 4 captive portal, Milestone 10's soak
    test, real-household-LAN/hardware fault testing) each need an actual
    decision or your physical presence — don't start any of them
    autonomously; report back and ask instead.

**2026-08-31: ran the full plan above, all six steps, clean.**

1. **Housekeeping.** `git pull --ff-only` fast-forwarded `218a489..d355563`
   as expected. The controller's 10MB memory limit from the prior
   session's OOM fault test had indeed survived (it was crash-looping,
   `Restarting (137)`) — `docker update --memory 0` silently no-ops on
   this Docker version (29.7.2) instead of actually clearing a limit
   (worth remembering: `docker update --memory 0` is not a reliable way
   to remove a limit once set); a `docker compose up -d --force-recreate
   controller` did the job properly (recreates from the compose
   definition, which carries no memory limit at all) and is now the
   preferred technique for this.
2. **Go test verification.** `phase3/nftables-manager`: `go build ./...`
   and `go vet ./...` both clean; `go test ./...` all green including
   both new `WriteHealth` tests, then a `-count=10` flake check on
   `internal/dbsource` — 10/10 clean. `phase3/arp-worker`: build/vet/test
   all clean too, nothing regressed. **The `WriteHealth` fix (commit
   `d53a8a8`) is now genuinely verified**, not just reasoned-through.
3. **Full Linux pytest run.** `447 passed in 90.55s`, 0 skipped — first
   confirmation of the full current suite on Linux.
4. **Live redeploy + smoke test.** `docker compose --profile interception
   up -d --build` rebuilt and restarted all six cleanly. `/health` showed
   both cards green ("running", recent timestamps) and controller logs
   stayed silent (adguard_sync.py's `sync_once()` is silent on success by
   design — only `on_error=lambda exc: log.warning("adguard sync failed:
   %s", exc)` in `controller/main.py` would have logged anything) across
   ~6 sync intervals (30s each) with zero "adguard sync failed" warnings.
5. **Fault-tested `nftables-manager`.** Two real findings here, one of
   which corrects this file's own earlier note above ("only a crash from
   inside the container's own PID 1" triggers `restart: unless-stopped`):
   - **`docker exec nftables-manager sh -c 'kill -9 1'` did NOT crash it
     at all** — confirmed via `/proc/1/status` before and after, process
     untouched. Root cause: `phase3/nftables-manager`'s (and, checked for
     comparison, `phase3/arp-worker`'s) Dockerfile `ENTRYPOINT` is the Go
     binary directly, no shell/tini/init wrapper — so the binary genuinely
     *is* PID 1 of its own PID namespace. Linux's kernel exempts init
     processes of a PID namespace from unhandled signals sent by a
     process *within that same namespace* — including SIGKILL — so a
     `docker exec`'d shell (which joins the container's existing PID
     namespace) simply cannot kill it this way, no matter the UID or
     capabilities (checked: both sender and PID 1 had identical
     `CapEff`/root). This means the technique this file previously
     documented as confirmed-working must have either been tested
     differently last time or never actually verified against a
     bare-binary-as-PID1 container — flagging rather than quietly
     re-asserting it.
   - **`docker kill -s KILL nftables-manager` from the host DID kill it**
     (real `ExitCode=137`, confirmed via `docker events`) — sent from an
     ancestor PID namespace with real privilege, so the kernel exemption
     doesn't apply. But exactly as this file's existing note predicted,
     Docker's restart-policy bookkeeping treats an explicit `docker
     kill`/`stop` as admin-intentional and does **not** auto-restart
     (`RestartCount` stayed put for 13+ seconds after).
   - **The sustained OOM-kill scenario is what actually worked as a full
     end-to-end test**, and needed retuning live: at a 10MB limit the
     process's real steady-state footprint (~7-8MB) mostly fit, only
     OOMing once right at a `docker update` boundary before settling
     back down (`docker events` showed a genuine `container oom` →
     `container die` (137) → `container start` cycle in under 300ms,
     `RestartCount` 0→1 — a real kernel-initiated OOM, correctly
     auto-recovered, unlike the admin-kill case above). Tightening to
     6MB produced a real sustained crash loop (`OOMKilled=true`,
     `RestartCount` stuck at 2 for 40+ seconds) — and during that window,
     **`/health` correctly showed the nftables-manager card as "stale —
     last reported healthy over 30s ago" instead of a false "running."**
     This is the live, end-to-end proof of the `WriteHealth` fix the plan
     was after, beyond the Go unit tests. Memory limit cleared via
     `--force-recreate` afterward; card confirmed back to green "running".
6. **Tear down.** All six containers' memory limits confirmed back to
   `0`. Found and removed one genuine leftover: the `ppfaulttest` Docker
   bridge network from a prior session's fault testing — **but this
   turned out to still be load-bearing**: `.env`'s `ARP_WORKER_IFACE`
   was still pointing at that network's `br-<id>` interface (a
   deliberately-fabricated sandbox network + fake `GATEWAY_IP`/
   `GATEWAY_MAC`, precisely so `arp-worker`'s real ARP-injection code has
   something safe to run against instead of this VM's real `eth0`/`ens1`
   or its cloud provider's actual gateway). Deleting the network broke
   `arp-worker`'s configured interface. Recovered by recreating the
   network with the identical name/subnet/gateway (`172.30.0.0/24`,
   gateway `172.30.0.1`), updating `.env`'s `ARP_WORKER_IFACE` to the new
   bridge's name (Docker assigns a new `br-<id>` per network even when
   the subnet is reused), and recreating `arp-worker`+`controller` to
   pick it up — confirmed clean startup and `/health` green again after.
   **Lesson for next time a Docker network needs tearing down on this
   VM**: check `.env` for any interface name that matches it first —
   this project's sandbox ARP-testing setup is real infrastructure the
   VM depends on, not disposable scratch state, even though its name
   sounds like a one-off.

Ended clean, all six containers up, `/health` green on both cards,
`git status` clean, no leftover memory limits — genuinely nothing left to
do autonomously per this plan's own stated exception. Not starting Phase
4/Milestone 10/real-LAN work without a decision from the user, per the
plan's own instruction.

### Fail-open engineering (a correction to an earlier assumption)

Linux neighbor-cache entries are a state machine, not a fixed TTL — a
stale mapping to a dead interception box can blackhole a client's
traffic for a real, bounded period after an *ungraceful* crash. A
*graceful* shutdown can proactively send corrective ARP replies; an
ungraceful one (SIGKILL, OOM, power loss) runs no shutdown code at all,
so corrective ARP is never in play for that case — recovery there
depends entirely on the client's own neighbor-cache retry logic, which
is exactly what the lease/heartbeat + supervisor-driven repair below
exists to bound. Worth being precise about since it's easy to
misread the switch-FDB finding two sections down as a crash-case
concern: it isn't, it's graceful-shutdown-only, since that's the only
path where corrective ARP code runs. The crash case is arguably a
touch worse than "just a stale ARP cache," though: active poisoning
itself (not just correctives) carries the same spoofed-L2-source
behavior, so a crash leaves the switch's own forwarding table pointing
at the dead worker too, alongside the client's ARP cache. Both get
fixed together by the same recovery event regardless — once the
client's neighbor-cache state machine gives up on the dead mapping and
sends a fresh broadcast ARP request, the real gateway's reply corrects
both the client's cache and the switch's table in one shot, since a
device's own transmission always carries its own honest MAC as the
wire-level source. This must be engineered before any testing against
the real home network, not added afterward:

- A lease/heartbeat between controller and worker — the worker stops
  forged refreshes and enters best-effort repair if the controller goes
  silent for several cycles, and never auto-resumes an old target
  generation after its own restart.
- A supervisor-driven independent repair path for the hard-crash case
  (`systemd Restart=on-failure` plus watchdog notifications — necessary
  but not sufficient on its own).
- An explicit ordered shutdown sequence: freeze policy updates → stop
  forged ARPs → send corrective ARPs → confirm representative clients
  resolve the real gateway MAC → remove/empty redirect rules atomically
  → leave the forwarding chain policy as `accept`.

**New finding (2026-08-30), from a real live test — a switch-level gap
corrective ARP alone doesn't close:** ran the actual `internal/worker`
packet-sending code against a real container on a Docker bridge network
(a genuine Linux L2 segment — see the ARP-worker README for why this,
not a cloud VNet, is the right free substitute for validating this
mechanism before touching a real LAN). Poisoning worked exactly as
designed: the victim's ARP cache flipped to the worker's MAC, and the
corrective shutdown sequence correctly restored the real gateway's MAC
in the victim's cache. But real connectivity to the gateway stayed
broken afterward. Root cause, traced to the byte level: `mdlayher/arp`'s
`Client.WriteTo` sets the **Ethernet frame's own source address** to
the ARP payload's claimed sender, not the worker's real interface MAC —
so a corrective reply (payload: "gateway is at gateway's real MAC")
is transmitted with that MAC as the literal L2 source too, teaching the
**switch's own MAC-forwarding table** that the real gateway now lives
on the worker's port. That's a different, lower-layer thing than a
victim's ARP cache, and corrective ARP alone doesn't fix it — confirmed
by inspecting the bridge's FDB directly (`bridge fdb show`) and seeing
the gateway's real MAC mis-pointed at the worker's port after "correct"
shutdown.

**Real-world blast radius, also confirmed empirically**: the instant
the real gateway container sent *any* frame of its own, the switch
immediately relearned the correct port and connectivity was restored —
switches relearn on every observed frame, not just ARP. A real home
router is constantly transmitting (routing all LAN traffic), so this
self-heals in about one packet's worth of time in practice, not the
"stuck" appearance an idle test container gave at first. Still a real,
previously-undocumented gap in what "corrective ARP" actually restores
— it fixes ARP caches, not switch forwarding tables, and the fix for
the latter is "wait for the real device to talk," not the worker's own
doing. Open call, not yet decided: whether it's worth making the
corrective phase use the worker's own real MAC as the Ethernet-layer
source (bypassing `mdlayher/arp`'s `WriteTo` for a hand-built frame)
so the switch relearns correctly immediately rather than relying on the
gateway's own traffic — given the confirmed sub-second self-healing on
an active gateway, this may not be worth the added complexity.

### Mesh (Orbi) validation — required before production use

No public documentation confirms whether the RBR850 filters unsolicited
ARP replies or how it handles MAC addresses across the wireless
backhaul to satellites. Must be validated directly, covering at least:
a device attached to the main router; a device attached to each
satellite; roaming between router and satellite and between satellites;
wired vs. wireless; DHCP renewal; satellite/firmware reboot; both
half-duplex and full-duplex poisoning modes. For each: confirm forged
ARP visibility, the client's resolved gateway MAC, traffic traversal in
both directions, no packet duplication/loops, and clean recovery after
a graceful stop, a hard kill, and a NIC-down event.

**No-go condition**: if a satellite-attached client receives forged ARP
replies but its actual unicast traffic is switched on a path that
bypasses the interception box, or Orbi rapidly overwrites the poisoned
entry, this architecture does not work as-is against this router and
needs rethinking before continuing.

Poisoning starts **half-duplex** (only the client's cache is poisoned)
to avoid fighting Orbi's own ARP table for its downstream client
entries; full duplex is only enabled if testing proves the reverse path
bypasses the interception box.

**Coexistence with Bark Home during active testing**: Bark Home is
presumably already ARP-spoofing this same LAN today (the basis for
believing ARP-spoofing protection is off). Two boxes spoofing the same
hosts at once would fight over ARP-cache entries with unpredictable
results, so **Bark Home will be paused for the duration of each active
test window** — confirmed feasible by the project owner. This means
active tests can run directly against the main LAN (including
satellite-attached devices) without the guest-network workaround, at
the cost of the household briefly losing Bark Home's protection during
each test window (mitigate by testing at low-stakes times, kept short).
Passive discovery (packet capture, `ip neigh` reads) has no such
conflict and doesn't require pausing anything — it doesn't put anything
on the wire.

### New database tables planned

- `device_bindings` — MAC/IPv4 pairs with first/last-seen timestamps,
  source, and confidence (`UNIQUE(mac_address, ipv4_address)`).
- `interception_runtime` — singleton runtime state: desired/applied
  generation, mode, last-healthy timestamp, fail-open reason.
- `network_events` — a normalized event log (event type, device, MAC/IP,
  source, timestamp, payload).

Identity rule: never auto-merge devices solely by hostname or vendor. A
MAC-randomization event creates a pending binding requiring explicit
user association — the same anti-evasion idea already planned for
Phase 4's captive portal.

### Discovery precedence

Kernel-native `rtnetlink` neighbor events (lowest latency) → periodic
`ip neigh` snapshot (catches missed events) → AdGuard query-log
observations (confirms active IP usage) → optional bettercap enrichment
(hostname/vendor only) → active, rate-limited ARP scan (only when stale
or onboarding a new device). Bettercap is not required for discovery at
all — only useful as an enrichment source if it's already running.

### Licensing

Bettercap (GPLv3) and Scapy (GPLv2), if used, are run as arms-length
subprocesses controlled via commands/JSON/REST — not imported as
libraries — which keeps this low-moderate legal risk for a public repo
under the FSF's own separate-programs-via-pipes-or-sockets guidance.
Scapy specifically is not used as an in-process library for this reason
(reinforcing the "compiled-language worker" choice above). eBlocker
(EUPL-1.2) is treated as a design reference only — its
`eblocker-network-tools` repo validates the "isolate privileged packet
emission behind message-based IPC" pattern, but its code isn't
reused directly; a small versioned JSON-over-Unix-socket protocol
replaces its Redis-based approach.

### Milestones

**2026-08-30: `arp-worker`, `nftables-manager`, and `controller` are all
containerized now** — until this date, every milestone below that says
"verified" meant "run by hand during a verification pass," never
actually deployable. All three now have Dockerfiles and are wired into
`docker-compose.yml`, gated behind a `profiles: ["interception"]`
compose profile so a plain `docker compose up` is completely unchanged
(proxy/adguard/dashboard only) — starting real interception is a
separate, explicit `docker compose --profile interception up -d`,
requiring real `ARP_WORKER_IFACE`/`GATEWAY_IP`/`GATEWAY_MAC` values with
no sensible default. See the new live-verification section after this
list for the full writeup, including three more real bugs found (a
silently-swallowed ARP send failure, a dead-worker-connection case only
a heartbeat could ever detect, a missing `iproute2` dependency) and the
fixes for each.

- [x] **1. Topology probe** — passive discovery, full Orbi attachment
      matrix, PCAP corpus. **Struck as not needed, 2026-08-31** (user
      decision): predates the pivot to ARP-spoofing-based interception,
      and every later milestone's discovery/identity work
      (`controller/discovery.py`, `rtnetlink_listener.py`,
      `active_scan.py`, the live veth-harness poisoning proofs) already
      covers what this item was meant to de-risk, without ever needing
      a full Orbi attachment matrix or a standalone PCAP corpus. Not
      attempted, not blocking anything downstream.
- [x] **2. ARP worker MVP** — static targets, half-duplex, corrective
      restoration on shutdown, unit-tested packet serialization.
      **Scaffold written 2026-08-29** in `phase3/arp-worker/` (Go): the
      generation scheduler, race-free corrective-restoration logic on
      generation switch, the lease/heartbeat state machine, startup
      target-safety checks, and the controller IPC protocol are
      implemented with unit tests for all of the above. **Builds,
      vets, and passes its full test suite on the smoke-test VM**
      (Go 1.26.7 auto-toolchain) as of the same day — one real API
      mismatch in the `mdlayher/arp` adapter found and fixed in the
      process (`netip.Addr` vs `net.IP`). Not yet wired into an actual
      controller process (Milestone 3, not started) or run against a
      real interface (needs `CAP_NET_RAW`, deliberately withheld until
      proven safe). See `phase3/arp-worker/README.md` for exact status.
- [x] **3. Controller** — versioned Unix-socket IPC, generations,
      leases, idempotent reconciliation.
      **Scaffold written and verified 2026-08-29** in `controller/`
      (Python, matching the architecture decision that the controller
      "fits the existing Python stack"): a `WorkerClient` speaking the
      exact same wire protocol as the Go worker's `internal/ipc`, an
      order-insensitive idempotent `reconcile()`, and a
      `HeartbeatPacer` keeping the worker's lease alive. Full test
      suite (288 tests including 20 new ones) passes on the
      smoke-test VM against real `AF_UNIX` sockets. Not yet a real
      deployable: `main.py`'s desired-state source is an explicit
      placeholder that raises rather than guessing, pending Milestone
      4's identity model.

      **Full pipeline verified together for real, 2026-08-30** — the
      first time Milestones 2/3/4/6 were run as actual processes
      together, not just independently: a real DB with a real device,
      the real `controller/main.py` process, the real
      `pp-arp-worker` binary, over their real Unix-socket IPC, on the
      same kind of Docker bridge network already proven to behave like
      a genuine L2 segment. Confirmed live: the controller computed
      desired state from the DB and told the worker to apply it; the
      worker actually poisoned a real container's ARP cache (verified
      against its real, independently-confirmed MAC — **2026-08-31
      update: a same-night re-test under Milestone 9 below initially
      cast real doubt on this claim, given a Docker-bridge-specific
      confound, then resolved that doubt in this claim's favor with a
      properly-isolated harness — read both dated notes there for the
      full trail, but the short version is this claim holds up**); the
      worker's own
      lease monitor detected the controller's death on its own and
      entered repair-only mode without being told to; a SIGTERM to the
      controller correctly propagated a real "shutdown" IPC message
      that made the worker send corrective ARPs, restoring the real
      gateway's MAC in the victim's cache; `interception_runtime`'s
      health columns updated correctly throughout. Found and fixed one
      real bug along the way: `-controller-uid=0` (a legitimate UID —
      root) was being rejected as "not provided," since the flag used
      0 as both its zero-value default and its required-check sentinel.
- [x] **4. Identity model** — `device_bindings`, outbox events, MAC/IP
      conflict handling. **Scaffold written and verified 2026-08-29**:
      `device_bindings`/`interception_runtime`/`network_events` tables
      added to `common/db.py`; `common/identity.py` records
      observations and handles both MAC/IP conflict shapes (IP
      reassigned to a new MAC, a device's own IP changing), each
      logged as a `network_events` row — never auto-associating a
      brand-new MAC to a `devices` row from network data alone.
      `controller/desired_state.py` replaces `main.py`'s placeholder
      with a real query (every non-ignored device with an active
      binding). 27 new tests, full suite verified at 298/298 locally
      and 305/305 on the smoke-test VM. **Discovery source added
      2026-08-29, wired into a running loop 2026-08-30**:
      `controller/discovery.py` implements the periodic `ip neigh show`
      snapshot (the design doc's "missed-event reconciliation" source)
      — parses real iproute2 output, records trusted entries via
      `identity.record_binding`, idempotent across repeated runs. This
      closed a real, previously-flagged gap (RoadMap.md itself,
      `docs/security/overview.md` §3): nothing was calling it
      regularly, so `device_bindings` — and therefore Squid's
      device-identity resolution and nftables policy computation —
      could go stale indefinitely after a DHCP renewal. `discovery.run_loop()`
      now drives `snapshot_once()` on a fixed interval via a new shared
      `controller/periodic.PeriodicTask` (factored out of
      `lease.HeartbeatPacer`, which is now a thin subclass of it — same
      tests, same behavior, no interface change), wired into
      `controller/main.py`'s `run()`/CLI (`--discovery-interval`,
      `--no-discovery`). Runs on its own background thread with its own
      DB connection, opened lazily ON that thread — a real bug in the
      first draft (a `sqlite3.Connection` built on the caller's thread
      raised `ProgrammingError` when used from the discovery thread) was
      caught by actually testing it, not just by inspection. Verified
      for real on the smoke-test VM: a genuine three-thread integration
      test (main reconcile loop + heartbeat pacer + discovery, one real
      `AF_UNIX` socket, one real second SQLite connection) plus the full
      365-test suite, both clean — this also caught and fixed a stale
      pre-existing test (`test_controller_run_cycle.py` still asserted
      the pre-`bump_v4` four-key `DesiredPolicy` dict; missed earlier
      because it's `AF_UNIX`-marked and silently skips on Windows, where
      that policy_state.py change had only been verified until now).
      **The higher-precedence live rtnetlink-event listener is built and
      verified now too (2026-08-30)**: `controller/rtnetlink_listener.py`
      uses `pyroute2` (pure Python, no C extension, the one deliberate
      exception to this package's stdlib-only convention --
      `controller/requirements.txt`) to react to real `RTM_NEWNEIGH`
      events within however long the kernel takes to deliver them,
      instead of waiting up to a full `--discovery-interval`. The real
      message shape (which address family is a genuine IPv4 ARP
      neighbor versus `AF_BRIDGE` FDB-learning noise or an IPv6 entry,
      and the integer `NUD_*` state bitmask instead of `ip neigh show`'s
      text names) was confirmed live against a real kernel before
      writing the filtering logic, not assumed -- `AF_BRIDGE` noise in
      particular dominates raw event volume on any Docker host and had
      to be filtered out. Deliberately reacts to `RTM_NEWNEIGH` only,
      never `RTM_DELNEIGH`, mirroring the snapshot loop's own "a binding
      goes stale by being replaced, never by absence" philosophy. Wired
      into `controller/main.py` as a fourth background task
      (`--no-rtnetlink` to opt out), on by default alongside
      `--db-path`. 28 new tests, 17 of them for the pure filtering logic
      and threading/retry wiring (faking `pyroute2` via `sys.modules`
      injection so they run on this project's Windows dev machine
      without it installed at all -- `pyroute2` is Linux-only, no
      `AF_NETLINK` on Windows).

      With both the snapshot and the live listener now running,
      staleness is bounded by whichever is faster for a given device
      (usually the live listener, sub-second) rather than by the
      snapshot's interval alone.

      **AdGuard query-log correlation built and verified live
      2026-08-31** (`controller/adguard_discovery.py`), closing one of
      the two sources this line used to flag as unbuilt: the real
      `/control/querylog` response shape was confirmed live against the
      VM's running AdGuard instance first (generating real DNS queries
      and reading the response back), matching this project's own
      "never trust docs alone" discipline — and it surfaced a real
      gotcha: AdGuard's `time` field carries variable-precision
      (commonly 9-digit/nanosecond) fractional seconds
      (`"2026-08-31T13:17:13.089285447Z"`), which compares unsafely as a
      plain string against this project's own fractional-second-free
      `db.now_iso()` timestamps — ASCII `.` sorts before `Z`, so a
      same-second fractional timestamp can compare as "earlier" than a
      whole-second one that's actually earlier in real time.
      `common/adguard_client.py`'s new `normalize_query_log_time()`
      truncates to whole seconds before anything is ever compared or
      stored. Since AdGuard's query log has no MAC (DNS carries no
      link-layer information), this source can only ever refresh an
      already-known binding's `last_seen_at`
      (`common/identity.py`'s new `touch_binding_by_ip`, never
      regressing it backward) — never create a new one, exactly
      matching this section's own "confirms active IP usage" wording.
      Verified end-to-end against the real live stack: a throwaway
      bridge-networked container's real DNS query through AdGuard was
      correctly correlated back to its `device_bindings` row (source
      became `adguard`, `last_seen_at` advanced to the query's real
      truncated timestamp) — and, as an unplanned bonus, this same test
      incidentally reconfirmed two OTHER pieces working correctly live
      together for the first time: the real snapshot discovery loop
      genuinely detected the throwaway container's actual MAC address
      on its own, and `record_binding`'s IP-conflict resolution
      correctly deactivated a synthetic test binding the moment the
      real one appeared for the same IP.

      **Active, rate-limited ARP scanning: done and verified live
      2026-08-31** (the design doc's final precedence-order source,
      "only when stale or onboarding a new device"). The design
      decision this needed (UDP-nudge vs. a new arp-worker IPC "probe"
      op) was confirmed live against a real kernel on the smoke-test VM
      **before** writing any code, per this project's own "verify
      against the real thing" discipline: deleting a neighbor entry for
      an address the host doesn't own and `sendto()`-ing a closed UDP
      port to it reliably transitioned the entry to `INCOMPLETE` (a
      genuine kernel-initiated resolution attempt), and doing the same
      against an already-STALE entry kicked off real re-verification —
      confirmed for both the "onboarding" and "stale refresh" shapes
      this feature needs, with no new `phase3/arp-worker` IPC op
      required. (One methodological dead end worth recording: the first
      version of this check nudged a *secondary IP address added to the
      same host's own interface* and found NO neighbor entry ever
      appeared at all — a locally-owned address is routed internally,
      never triggering real ARP. Re-ran against a genuinely unowned
      address instead, which is the valid test.)

      `controller/active_scan.py` (`select_stale_bindings()`,
      `nudge()`, `scan_once()`, `run_loop()`) mirrors
      `adguard_discovery.py`'s shape exactly, wired into
      `controller/main.py` behind `--active-scan-interval`/
      `--active-scan-stale-after`/`--active-scan-limit`/
      `--no-active-scan`. It deliberately only ever nudges — it never
      writes `device_bindings` itself; `controller/discovery.py`'s
      already-running snapshot loop is what observes and records any
      resulting resolution, exactly like `adguard_discovery.py`'s own
      narrow-scope precedent. 12 new fully-mocked unit tests (no real
      network) cover staleness/rate-limit selection, the nudge itself,
      and `run_loop` wiring — see `tests/test_controller_active_scan.py`.

      **Live end-to-end proof, using a genuinely separate network
      namespace** (the veth harness below, built for this and reused
      for the gateway-reboot test) rather than a self-owned IP: a
      `device_bindings` row was seeded with `last_seen_at` an hour in
      the past (source `rtnetlink`) for a real container on the far
      side of a veth pair, with its host-side neighbor entry deleted
      first for a clean baseline. Running the real `active_scan.scan_once()`
      sent one real UDP datagram across the bridge; one second later
      `ip neigh show` showed the container's real MAC in `REACHABLE`
      state (a genuine ARP resolution, not a self-answered loopback).
      Running the real `discovery.snapshot_once()` immediately after
      picked that up and rewrote the binding: `last_seen_at` jumped from
      the seeded hour-old value to "now," `source` became `snapshot` —
      the full nudge-to-discovery pipeline closing end-to-end, for real,
      exactly as designed.

      Deployed live to the production controller the same session
      (`docker compose up -d --force-recreate controller` after
      rebuilding the image) — `interception_runtime` stayed `running`
      through and past the first scan interval, no crash, no fail_open.
      Two `device_bindings` rows that this session's own testing had
      caused the *production* controller's live discovery/rtnetlink
      loops to pick up (a real, useful reminder: `arp-worker`/
      `controller` both run `network_mode: host`, so anything visible in
      the host's own `ip neigh` table — including scratch veth-harness
      traffic — is visible to production's discovery loops too) were
      found and deactivated (`active = 0`, not hard-deleted) before
      redeploying, rather than left to linger as bogus pending bindings.
- [x] **5. `nftables` integration** — dedicated table, named policy
      sets, atomic apply/rollback. **Scaffold written AND verified
      against real nftables 2026-08-29**, in `phase3/nftables-manager/`
      (Go, `sigs.k8s.io/knftables`): pure conflict-resolution
      (`ResolveConflicts` — an IP requested in more than one policy set
      keeps only its highest-priority one, matching the prerouting
      chain's own evaluation order) and diffing (`Reconcile`) logic,
      fully unit tested; a thin adapter that builds the exact table
      from the design skeleton. Unlike the ARP worker, this piece could
      be functionally verified for real with no special hardware — a
      plain `docker run --cap-add=NET_ADMIN` container gets its own
      nftables state in its own network namespace. Verified: bootstrap
      produces the exact skeleton ruleset; an atomic apply of a diff
      lands correctly; re-reconciling against unchanged desired state
      against a *real* kernel ruleset produces an empty diff; an
      incremental add+remove applies as one atomic transaction. A
      reconciliation loop and real desired-state input (Milestone 7,
      below) were wired up later the same day, and `EnsureBaseline` was
      fixed to be safe across repeated calls (a restart no longer
      duplicates the redirect rules — `knftables`'s `Add()` is
      idempotent for tables/sets/chains but not rules, which it always
      appends; fixed with a chain `Flush()` before re-adding, verified
      both against `knftables.Fake` and live against real nftables
      bootstrapped twice in a row).
- [x] **6. Service health** — Squid/AdGuard/controller readiness gates,
      systemd watchdog + restart limits. **Done and verified 2026-08-29**:
      `common/sdnotify.py` (stdlib-only systemd sd_notify client,
      READY=1/WATCHDOG=1) wired into the controller's heartbeat pacer;
      `controller/health.py` writes `interception_runtime`'s
      `mode`/`last_healthy_at`/`fail_open_reason` — the first real use
      of that table since Milestone 4 added it. A failed reconcile
      cycle is now logged and reported as `fail_open` rather than
      crashing the process.

      **Readiness gates built and verified live 2026-08-31**
      (`controller/readiness.py`), closing the one real gap this line
      used to flag: `wait_for_worker()` retries connecting to the ARP
      worker's Unix socket for a bounded timeout (30s default) instead
      of raising on the very first attempt, turning the ordinary
      "arp-worker hasn't created its socket file yet" startup race
      (docker-compose.yml's own comment already documented this as an
      accepted one-restart-cycle gap) into a fast in-process retry
      instead of a full container restart — genuinely raises past the
      timeout, so a truly-broken worker still surfaces the same way it
      always did. `wait_for_adguard()` is a bounded, best-effort gate
      before starting the periodic sync loop; deliberately never raises
      (AdGuard isn't required for the rest of `run()` to function, and
      its own periodic sync already retries forever on its own
      schedule) — a real application of this project's fail-open
      philosophy to a startup concern, not just steady-state behavior.
      Both wired behind new optional CLI flags
      (`--worker-ready-timeout`/`--adguard-ready-timeout`) with no
      change to callers that don't pass them.

      **Health reporting extended 2026-08-31** to close a real gap a
      Milestone 9 NIC-down test found: `interception_runtime` used to
      stay `'running'` throughout a genuine, sustained ARP-send-failure
      window, since it only ever tracked the controller↔worker
      Unix-socket heartbeat, not the worker's actual packet
      transmission. Full writeup, including the live before/during/
      after `/health` proof, is under Milestone 9 below.
- [x] **7. Authentication workflow** — toggling
      `devices.is_authenticated` updates policy without restarting
      spoofing. **Done and verified live end-to-end 2026-08-29** — see
      Milestone 5's update above and `controller/policy_state.py`:
      built a real DB with three devices in three policy states, ran
      the real `pp-nftables-manager` binary in a
      `--cap-add=NET_ADMIN` container against it, then — confirmed via
      `docker inspect StartedAt` staying constant, i.e. **no restart**
      — toggled one device's `is_authenticated` from the host and
      watched the same running process move that device's IP from
      `unauthenticated_v4` to `authenticated_v4` in the real kernel
      ruleset on its next poll cycle. This is the milestone's exact
      claim, proven against real components.
- [x] **8. Future-portal seam** — implement the `PolicyClass` enum
      (`AUTHENTICATED` / `PREAUTH` / `BYPASS` / `QUARANTINE`) now, even
      though only the first two are used until Phase 4. **Done
      2026-08-29**: `common/policy_class.py`'s `PolicyClass` enum +
      `classify_device()`, precedence `bypass > quarantine >
      authenticated/preauth` matching the nftables chain's own
      evaluation order. `devices.quarantined_at` added (nullable,
      nothing sets it yet — no dashboard control exists to trigger
      quarantine; that's future, user-facing work, not built here).
- [x] **9. Fault campaign** — signals, OOM kill, NIC down/up, gateway
      reboot, DB lock, malformed IPC, partial `nftables` failure. **All
      sub-items done and verified live as of 2026-08-31** (gateway
      reboot, the last one, closed the same day — see its own dated
      entry below). Starting from 2026-08-29's subset testable without
      real network hardware or destructive host access: malformed IPC
      (covered since Milestone 2/3's dispatch tests); DB lock (a
      transient SQLite lock from a concurrent writer just makes a
      health write wait out `busy_timeout`, doesn't crash — tested);
      partial `nftables` failure (proven a non-issue by construction:
      `knftables.Run()` is atomic so the kernel can never be left
      half-updated, and `internal/nft`'s fault tests plus the
      reconciliation loop's read-fresh-every-cycle design mean a
      process crash mid-cycle self-corrects on the next tick, no
      special resume logic needed); **the worker connection itself
      dying** — previously just a documented gap — the controller
      (`controller/main.py`) now detects a dead `WorkerClient`
      (`WorkerConnectionError`, distinct from an application-level
      fault reply) and reconnects on its own, verified with a real
      integration test (real signals, real threads, a simulated worker
      crash-and-restart, a clean `SIGTERM` shutdown afterward). Building
      that test surfaced and fixed a real, previously-unnoticed bug:
      `WorkerClient` had no lock, so the heartbeat-pacer thread and the
      main reconciliation loop could genuinely race on the same socket
      — fixed with a `threading.Lock`, verified with 40 concurrent
      calls from 40 threads.

      **OOM kill: done and verified live 2026-08-31**, against
      `nftables-manager` specifically (see the dated VM-verification
      writeup earlier in this file) — a genuine kernel OOM kill,
      confirmed via `docker events`, correctly auto-recovered by
      `restart: unless-stopped`, with `/health` correctly showing
      "stale" during the down window.

      **NIC down/up: partially investigated 2026-08-31, real findings,
      not fully resolved.** This line previously assumed toggling a
      network interface needed real hardware or a network-namespace
      harness this project didn't have — checked that assumption
      directly instead of continuing to assert it: a plain
      unprivileged VM account (no host sudo) CAN bring the sandbox
      bridge interface down and up via `docker run --network host
      --cap-add=NET_ADMIN alpine ip link set <iface> down/up` — Docker
      group membership alone is enough, no host root needed. Toggling
      it while `arp-worker` had a real active poisoning target (a
      throwaway victim container, a real `devices`/`device_bindings`
      row) produced a real, confirmed effect: `worker.Config.OnSendError`
      fired for real (`"ARP send failed ... sendto: network is down"`,
      the exact log line `cmd/pp-arp-worker/main.go` wires it to) —
      proving that code path is real and observable, not silently
      swallowed. What this pass could NOT conclusively confirm: whether
      poisoning genuinely resumes once the interface returns. Checking
      the victim's own ARP cache afterward showed the gateway IP still
      resolving to the sandbox bridge's own real MAC, not the
      configured spoofed `GATEWAY_MAC` — which may mean poisoning
      hadn't yet resumed, or may just mean a Linux bridge's own
      near-instantaneous ARP handling for its own gateway IP reliably
      outraces an injected spoof reply in a virtualized Docker-bridge
      network specifically, a fidelity question distinct from (and
      more fundamental than) this fault test. **Also unconfirmed**:
      whether the controller<->worker heartbeat itself (which continued
      reporting "healthy" throughout, since it runs over the Unix
      socket IPC, entirely independent of the poisoned network
      interface) means a NIC-down condition is currently invisible to
      `/health` even while real poisoning is failing — plausible given
      what was observed, but not independently isolated from the
      poisoning-fidelity question above. Left as a genuinely open,
      tracked item rather than resolved either way; the next pass
      should isolate these two questions (does poisoning provably
      resume without a worker restart; is a NIC-down condition visible
      anywhere in health reporting) with a purpose-built test rather
      than an improvised one.

      **Gateway reboot: done and verified live 2026-08-31** — see the
      dedicated writeup immediately below (including a real correction
      to the naive "just `docker stop`/`docker start` the stand-in"
      plan this paragraph originally proposed).

      **Gateway reboot: done and verified live 2026-08-31.** Rebuilt the
      veth harness (below) and ran a real device/binding-backed
      poisoning target against it. **Real, useful finding along the
      way, discovered before the reboot test itself could even run**: a
      plain `docker stop`/`docker start` of a `--network none` container
      does NOT behave like a real gateway reboot — Docker tears down and
      recreates that container's entire network namespace on stop/start,
      silently destroying every manually-injected veth/IP this harness
      relies on (confirmed directly: `ip addr show` inside the
      "restarted" gateway stand-in came back with only loopback, and its
      bridge-side veth peer had vanished too). A real gateway reboot
      keeps its interface/MAC identity the whole time; `docker stop`/
      `start` does not, so it's the wrong tool for this test. Switched
      to bringing the gateway stand-in's own veth link down, waiting,
      then back up (`ip link set veth-g down` / `up` from a privileged
      helper joined to its netns) — same real "device goes silent, comes
      back with the same identity" fault shape, without Docker's
      namespace-recreation confound.

      With that corrected technique: poisoning was confirmed live
      first (victim resolved the worker's own MAC), then the gateway
      stand-in's link was brought down for 3s and back up. Result: zero
      worker crashes, zero controller reconnects/fail_opens, poisoning
      completely unaffected throughout and after — confirming the
      milestone's own long-standing expectation ("poisoning doesn't
      depend on the real gateway being reachable at all") for the first
      time with a real result rather than an assumption. A final forced
      fresh-resolution check afterward showed the same
      always-real-reply-wins-a-race / periodic-reannouncement-wins-
      afterward pattern as every other test this project has run against
      this harness.

      **One more real, unplanned finding from this same session**:
      because `arp-worker`/`controller` both run `network_mode: host`,
      the production controller's own live discovery/rtnetlink loops
      were watching the SAME host `ip neigh` table this throwaway veth
      harness (and an earlier ad-hoc UDP-nudge check) used, and picked
      up two bogus `device_bindings` rows from the test traffic as a
      real side effect. Found and deactivated (`active = 0`) before
      moving on -- worth remembering for any future host-netns test
      harness on this VM: it is not actually isolated from production's
      own passive discovery, even though it never touches the
      production DB or containers directly.

      **Exact reusable runbook** (condensed from the two builds this
      project has now done against this technique; all resources are
      throwaway and get deleted after, nothing here touches the
      production `ppfaulttest`-based stack):
      1. Bridge with **no IP conflicting with the gateway IP** (the
         bridge needs SOME IP for `mdlayher/arp`'s client to bind, but
         it must be disjoint from whatever IP the gateway stand-in
         gets, or the bridge itself becomes an unwanted ARP-answering
         L3 participant again — the exact bug this whole harness was
         built to avoid):
         `docker run --rm --network host --cap-add=NET_ADMIN alpine:3.20 sh -c 'apk add --no-cache iproute2; ip link add br-arptest type bridge; ip link set br-arptest up; ip addr add 10.7X.0.99/24 dev br-arptest'`
      2. Two `--network none` containers (victim, gateway stand-in).
         For each: `docker run -d --name pp-<role> --network none alpine:3.20 sleep 900`,
         note its PID (`docker inspect --format '{{.State.Pid}}'`).
      3. One veth pair per container, one end mastered into the bridge,
         the other moved into that container's netns by PID, all from
         one `--network host --pid=host --cap-add=NET_ADMIN` helper —
         see tonight's exact commands in this session's history if the
         condensed form here isn't enough; the technique is fully
         proven, just mechanical to repeat.
      4. Assign IPs inside each container via
         `docker run --rm --network container:pp-<role> --cap-add=NET_ADMIN alpine:3.20 sh -c 'ip link set lo up; ip link set veth<X> up; ip addr add 10.7X.0.N/24 dev veth<X>'`.
      5. Throwaway worker + controller, same production images, fresh
         socket/DB volumes, pointed at the new bridge and the gateway
         stand-in's real IP/MAC:
         `docker run -d --name pp-test-worker --network host --cap-add=NET_RAW -v <sockvol>:/run/test parental_proxy-arp-worker:latest -iface=br-arptest -socket=/run/test/worker.sock -controller-uid=0`
         then `docker run -d --name pp-test-controller --network host -v <sockvol>:/run/test -v <dbvol>:/testconfig parental_proxy-controller:latest --socket=/run/test/worker.sock --db-path=/testconfig/test.db --gateway-ip=<gw-ip> --gateway-mac=<gw-real-mac> --no-discovery --no-rtnetlink`.
      6. Insert one real device/binding row for the victim directly via
         `identity.record_binding(conn, mac, ip, source='snapshot')`
         (see this session's own DB-insert snippets) so it becomes a
         real poisoning target.
      7. **For the gateway-reboot test specifically**: once poisoning is
         confirmed live (victim resolves the worker's MAC), do **NOT**
         use `docker stop`/`docker start` on the gateway stand-in — see
         this section's own finding above for why that destroys its
         network namespace instead of simulating a reboot. Use
         `docker run --rm --network container:pp-<gateway-role>
         --cap-add=NET_ADMIN alpine:3.20 sh -c 'apk add --no-cache
         iproute2; ip link set veth-<X> down'`, wait a few seconds, then
         the same with `up`. Watch `docker logs pp-test-worker`/
         `pp-test-controller` throughout for anything unexpected
         (crashes, IPC faults) — confirmed result (2026-08-31): nothing
         happens, poisoning is completely unaffected, since it doesn't
         depend on the real gateway being reachable at all. Also confirm
         the victim's own resolution behavior stays sane once the
         gateway stand-in returns (a forced fresh resolution, same
         technique as the NIC-down investigation, shows the real
         restored MAC then gets re-poisoned within one interval, same as
         every other result this harness has produced).
      8. Use `nicolaka/netshoot` + `nsenter -t <PID> -n ip neigh ...`
         for any real inspection/manipulation of a container's ARP
         table from outside it — Alpine's own `iproute2` package is
         just a busybox-applet shim with no real `neigh del` support,
         confirmed the hard way tonight.
      9. Clean up after: `docker rm -f` every throwaway container,
         `docker volume rm` both volumes, `ip link del br-arptest` (via
         the same privileged-helper technique). Confirm the production
         stack's `/health` is still green before and after, same as
         every other test tonight.

      **Follow-up the same night: isolated the NIC-down/up question from
      a deeper, more consequential one, and found real cause for doubt
      in Milestone 3's own "worker actually poisoned a real container's
      ARP cache" claim below.** Set up a clean baseline with NO
      interface toggling at all: a real victim container, a real active
      poisoning target, generation applied and confirmed (`applied
      generation N (1 targets)`), then explicitly deleted the victim's
      existing ARP entry for the gateway and forced a genuinely fresh
      resolution (a real `ping`, via a helper container joined to the
      victim's network namespace with `CAP_NET_ADMIN` since the victim
      itself has none). Result, repeated and consistent: the victim
      resolves the gateway IP to the sandbox bridge's OWN real MAC
      every time, never the configured spoofed `GATEWAY_MAC` — even
      though the worker is actively, continuously applying that target
      (confirmed via the controller's own generation-count logging).

      **Why, and what this means**: `docker network create` gives the
      bridge device itself an IP (`172.30.0.1` here) and Linux bridges
      answer ARP for their own IP synchronously, in-kernel, with
      effectively zero latency — a fundamentally different situation
      from a real LAN, where the gateway is a separate physical device
      reachable only over the wire, which is exactly the latency gap
      ARP-spoofing techniques rely on to win the race. A Docker bridge
      is not just "a genuine L2 segment" (this file's own 2026-08-30
      claim, made without a documented test forcing a fresh resolution
      the way this pass did) — it is also, simultaneously, an
      authoritative, instant ARP responder for the address being
      spoofed, which no real gateway is from the worker's point of
      view. This casts real doubt on the 2026-08-30 Milestone 3 entry's
      "the worker actually poisoned a real container's ARP cache"
      claim: the most likely honest explanation, given tonight's
      result, is that the worker's gratuitous ARP replies genuinely DID
      land in the victim's neighbor cache for a transient window (a
      real effect, not a fabricated one) simply because nothing had
      forced a genuine re-resolution in the narrow window that claim
      was checked in — not evidence that poisoning holds up durably
      against real, ongoing ARP traffic the way the feature actually
      needs it to. That claim is being corrected here, not rewritten,
      per this project's own established practice.

      **Net effect: this VM's Docker-bridge sandbox cannot conclusively
      validate the core ARP-poisoning mechanism at all**, in either
      direction — not the original Milestone 3 claim, not tonight's
      NIC-down/up recovery question, not gateway reboot. All three need
      a fundamentally more faithful harness before any of them can be
      called verified: e.g. two Linux network namespaces joined by a
      plain veth pair with no bridge (and therefore no in-kernel
      ARP-answering device) between them, so a genuine peer with real
      wire-clock latency stands in for the gateway, or real hardware.
      Building that harness is real, non-trivial work and a genuine
      design decision (namespace/veth topology, how `arp-worker`'s
      `ARP_WORKER_IFACE`/`GATEWAY_IP`/`GATEWAY_MAC` map onto it) —
      flagged to the user rather than started autonomously.

      **Same night, immediately after: built that exact harness, and
      the news is good — the poisoning mechanism genuinely works,
      including recovering from a NIC flap.** Two throwaway containers
      (`--network none`) joined by two veth pairs plugged into a plain
      Linux bridge with **no IP address of its own** (`ip link add
      br-arptest type bridge` — deliberately never `ip addr add` on the
      bridge device itself, the one thing the earlier confound
      required), one side holding the "victim" role, the other a
      genuine, independently-listening "real gateway" stand-in — plus
      a throwaway `arp-worker`+`controller` pair (fresh socket path,
      fresh SQLite DB, same production images, one real device/binding
      row for the victim) pointed at this topology instead of the
      shared `ppfaulttest` sandbox. Building it required nothing beyond
      what the earlier feasibility check already proved (creating a
      veth pair and moving ends into container namespaces by PID, all
      via `docker run --network host --pid=host --cap-add=NET_ADMIN`,
      no host root) plus one more small technique:
      `nicolaka/netshoot` + `nsenter -t <pid> -n` to run genuine,
      full-featured `ip neigh` commands inside a container's namespace
      from outside it (Alpine's own `iproute2` package turned out to
      still just be a busybox-applet shim with no real `neigh del`
      support — worth remembering for next time).

      **Results, each repeated and consistent:**
      - Steady-state poisoning: the victim's ARP entry for the gateway
        correctly and stably showed the worker's own MAC, not the real
        gateway's.
      - **The actual mechanism, traced precisely**: a genuinely fresh
        resolution (explicit `ip neigh del` + a real ping) is won by
        the real gateway stand-in every time — a live, listening peer
        replies to a real ARP request just as fast as anything else on
        a virtualized segment, so the worker was never going to win
        that specific race. What actually works is the OTHER half of
        the design: the worker's own periodic (`Interval: 2s`)
        gratuitous, unsolicited ARP re-announcement, which the kernel
        accepts as an update to an already-resolved entry — re-poisoning
        it back to the worker's MAC within about one interval, every
        time, holding indefinitely until the next genuine re-resolution
        forces a brief, expected flicker back to the truth. This is
        exactly the mechanism the design was always supposed to rely
        on (RoadMap.md's own Phase 3 design section on continuous
        re-poisoning, not a one-shot poison) — now actually proven, not
        assumed.
      - Graceful shutdown: `docker stop` (SIGTERM) → the worker's own
        logged "shutting down: sending corrective ARPs before exit" →
        the victim's entry was restored to the REAL gateway's MAC
        **immediately**, with no fresh-resolution trigger needed.
      - **NIC down/up, now properly answered**: brought `br-arptest`
        down while poisoning was live — the worker correctly kept
        trying every cycle and logged a real, distinct `OnSendError`
        each time (`sendto: network is down`), never crashing. Brought
        it back up — no further failures logged (this project's
        established "silent on success" pattern), and a forced fresh
        resolution afterward confirmed the worker correctly re-poisoned
        the entry again within one interval, with **no process restart
        needed**. Both halves of what was an open question are now
        closed with real evidence, not assumption.
      - **The one genuinely confirmed gap, now proven rather than
        speculated**: `interception_runtime` in the throwaway DB read
        `mode: 'running'`, `fail_open_reason: None` throughout the
        entire NIC-down window — health reporting is blind to this
        failure mode, because it only tracks the controller↔worker
        Unix-socket heartbeat, which the LAN-facing interface going
        down has no effect on whatsoever. `/health` would show fully
        green while the actual interception mechanism is completely
        failing. **Fixed and verified live the same night** — see
        immediately below.

      **Fixed the health-visibility gap above, verified live end-to-end
      2026-08-31.** `worker.Worker` gained a global, atomic
      `consecutiveSendFailures` counter (incremented on every failed
      `ARPSender.Reply()`, reset to 0 on any success — global rather
      than per-target, since the motivating failure fails every send at
      once regardless of target), reported to the controller on every
      `heartbeat_ack` via a new `HeartbeatAck.ConsecutiveSendFailures`
      field — closing a gap `sendGratuitousReply`'s own long-standing
      TODO comment had already named exactly ("SUSTAINED failure should
      still escalate to a controller message... needs a
      failure-rate/consecutive-failure counter, not built here"). The
      controller's heartbeat pacer writes the latest value into a small
      shared dict; `run_cycle` (the sole writer of
      `interception_runtime`'s `mode`/`fail_open_reason` columns, kept
      that way deliberately to avoid two threads racing on the same
      write) reports `fail_open` instead of `running` once 3 or more
      consecutive failures are seen, with a clear, specific reason
      string. Caught and fixed one real bug along the way, via the
      fix's own integration test run for real on the VM rather than by
      inspection: `health.report_fail_open()` never touched
      `applied_generation`, correct for its original callers (the
      reconcile cycle itself failed, no fresh generation to report) but
      wrong for this new caller, where reconciliation genuinely
      succeeded — a bare `report_fail_open()` on a brand-new row let
      `applied_generation` silently default to 0, understating the real
      value on the very first `fail_open` cycle. Fixed with an optional
      `applied_generation` parameter, defaulting to `None` (preserving
      every existing caller's behavior exactly) and set explicitly by
      the new caller.

      **Verified live, twice**: `go build`/`go vet`/`go test` clean
      (2 new tests, 10× flake-checked) plus the full Linux `pytest`
      suite (482 passed, 0 skipped, up from 474). Then rebuilt the
      images and re-ran the exact veth-harness NIC-down scenario from
      the investigation above end to end: baseline `mode: 'running'` →
      interface down → **`mode` flipped to `'fail_open'` within 2
      seconds**, with a live-incrementing, human-readable reason
      (`"arp-worker: N consecutive ARP send failures (the bound network
      interface is likely down)"`) and `applied_generation` correctly
      preserved at its true last-known-good value throughout → interface
      restored → `mode` correctly returned to `'running'`,
      `fail_open_reason` cleared. The production stack (`ppfaulttest`-
      based) was rebuilt and redeployed with this fix too, confirmed
      still green throughout, and all throwaway resources removed
      after.

      **This also substantially restores confidence in the original
      2026-08-30 Milestone 3 claim** this section corrected earlier
      tonight: it was likely a real observation after all, just an
      unrigorous one (no forced fresh-resolution check) that happened
      not to get unlucky. Tonight's harness is the rigorous version of
      that same claim, and it holds up. All disposable resources
      (containers, the bridge, both Docker volumes) removed after;
      the production `ppfaulttest`-based stack was never touched and
      stayed healthy throughout (`/health` green on both cards the
      whole time, confirmed before and after).
- [ ] **10. Soak test** — 7–14 days of mixed real household load,
      roaming, sleep/wake, with memory/FD/CPU trend monitoring. **Not
      startable autonomously** — needs the real household network
      running this stack for real, over real time, which is squarely
      the project owner's call on when to begin (deploying an
      ARP-spoofing daemon to the live LAN is exactly the kind of step
      this project's own testing discipline says shouldn't happen
      without them directly involved). **Explicitly gated on two things
      the user confirmed 2026-08-31**: pushing this stack to the real
      production box, and shutting down the household's current Bark
      Home setup (the thing this project replaces) — both are the
      user's own infrastructure decisions, not something to plan around
      autonomously. Everything upstream of this (Milestones 1–9) is
      ready to support a soak test whenever those two things happen;
      nothing else can be done to move Milestone 10 forward before then.

Milestones 1–9 above (all but the soak test) have real, tested — several
functionally verified against real nftables/real sockets/real subprocess
behavior, including the controller and nftables-manager coordinating
live through the shared DB (Milestone 7's end-to-end proof) — work
behind them. As of 2026-08-30: the discovery daemon is wired into a
running loop with the higher-precedence live rtnetlink listener on by
default (see above), the dashboard "interception health" view reading
the tables above is built (see "Dashboard 'interception health' view..."
below), and this has run against real network interfaces inside
disposable VMs/containers. What's NOT built/done: a soak test
(Milestone 10, deliberately owner-gated), and running any of this
against a REAL household LAN and real hardware NIC (`CAP_NET_RAW`/
`CAP_NET_ADMIN` have only ever been granted inside a disposable VM or
container so far, never on the real production Beelink box).

---

## Phase 4 — Captive-portal forced enrollment (begun 2026-08-31)

**Foundational gap found while scoping the first real milestone, before
any code was written**: today, a genuinely brand-new MAC (one
`common/identity.py`'s `record_binding()` has never seen
before) gets a `device_bindings` row with `device_id = NULL` --
deliberate, per Milestone 4's "never auto-merge devices solely by
hostname/vendor" rule. But `controller/desired_state.py`'s
`db_backed_desired_state()` `JOIN`s `device_bindings` to `devices` on
that same `device_id` -- a `NULL` device_id produces no row at all in
that query, so a brand-new device isn't merely "not yet gated," it is
**not an ARP-poisoning target of any kind**, meaning it gets full,
unfiltered internet access, invisible to this project's own
interception layer entirely, until a human manually creates and
associates its `devices` row from the dashboard. Even if a `devices`
row existed, `devices.is_authenticated` defaults to `1` in the schema
("no login gate yet to fail" -- `common/policy_class.py`'s own
docstring), which would classify it `AUTHENTICATED`, not `PREAUTH` --
the opposite of "gated by default."

This makes the actual first Phase 4 milestone **auto-creating a real,
unassociated `devices` row (`is_authenticated = 0`, `ignored = 0`, no
`user_id`) the moment any discovery source observes a genuinely new
MAC**, rather than leaving it a dangling `device_id = NULL` binding --
this is a new devices row created fresh for a MAC nothing has ever
seen, not merging a MAC into an *existing* devices row by any kind of
heuristic guess, so it doesn't conflict with Milestone 4's
never-auto-merge rule; it resolves the never-auto-merge rule's own
loose end (an association that previously required a human, indefinitely).
Doing this correctly makes such a device classify as `PREAUTH`
immediately, land in nftables' `unauthenticated_v4` set on the very
next reconcile cycle (DNS-tier visibility/control from the moment it's
first seen), and become gate-able by the portal login flow below --
without this, the portal has nothing to gate in the first place.

**Milestone 1 (auto-gate new devices): done and verified 2026-08-31**,
`common/identity.py`'s `record_binding()` -- see the description above,
which is the finished design, not just the plan. 4 new tests, two of
which are true end-to-end proof (a bare `record_binding()` call with no
`devices` row pre-created lands a real target in nftables'
`unauthenticated_v4` set). Grandfather clause confirmed by its own
dedicated test: an already-known-but-unassociated MAC from before this
shipped stays untouched even across a later DHCP renewal.

**Milestone 2 (dashboard visibility for the new PREAUTH state): done
and verified 2026-08-31.** `dashboard/dashboard.py`'s `/devices` page
now computes a `pending` flag per device (`ignored = 0 AND
bypass_login = 0 AND is_authenticated = 0`) and sorts pending rows
first; when any exist, a highlighted "Devices awaiting login" card
appears above the Groups card -- same `.pending-card` visual convention
the Report page's own "Pending approval requests" card already
established, not a new pattern -- listing each one with a one-click
**Bypass** action alongside the existing **Manage** link. The main
devices table also gained a **Status** column (Awaiting login /
Authenticated / `—` for ignored/bypassed) so the state is visible even
outside the summary card. New `POST /devices/bypass_login` route
(`bypass_login_device()`) deliberately only ever touches the one
column -- unlike `update_device()`'s wholesale form resubmit, this lets
the pending-card's single-button action fire without risking blanking
a device's label/assignment. 7 new tests in `tests/test_dashboard.py`,
plus a real visual/interactive check in a live browser against this
repo's own pre-existing `dashboard/dev_server.py` local launcher: the
pending card, Status badges, and the full Bypass round-trip (device
leaves the pending list, `bypass_login` flips to `1`) all confirmed
exactly as designed.

**Milestone 3 (the actual forcing mechanism -- captive-portal probe
interception): done and verified 2026-08-31.** Real, useful finding
*before* any code was written: the nftables side of this needed ZERO
changes -- `phase3/nftables-manager/internal/nft/knftables_adapter.go`'s
`baselineRules` has carried
`ip saddr @unauthenticated_v4 tcp dport 80 redirect to :3131` since
Phase 3 was first designed, with a `# -> future portal` comment in the
original design doc. This milestone is that future portal.

**Design decision, resolved by looking up how OS captive-portal
detection actually works** (Apple `captive.apple.com/hotspot-detect.html`
expects the literal string "Success"; Android/Chrome
`.../generate_204` expects a bare 204; Windows
`www.msftconnecttest.com/connecttest.txt` expects "Microsoft Connect
Test"; Firefox `detectportal.firefox.com/success.txt` expects
"success" -- see [Apple's own forum](https://developer.apple.com/forums/thread/747798),
[a maintained captive-portal-URL reference](https://gist.github.com/mortonfox/c31de2b3ac967edb089e9bbd3dbe23a2),
and [Mozilla's own captive-portal docs](https://firefox-source-docs.mozilla.org/networking/captive_portals.html)):
since nftables redirects by source IP + destination PORT, not by
hostname, EVERY one of these different-hostname probes lands on the
same server -- and getting anything other than each one's exact
expected response is already what every one of them treats as "there's
a captive portal," at which point each opens (or offers to open)
exactly the probe URL it just tried in a real browser/webview, which
lands right back here again and renders whatever HTML comes back. That
means one handler returning the SAME login page for every GET
regardless of path/Host is enough to trigger every major OS's native
sign-in UI AND show the form to a device manually browsing -- no
per-OS response-shape special-casing needed at all.

Built `dashboard/captive_portal_server.py` (started from
`dashboard/dashboard.py`'s `main()`, same `network_mode: host` process
as `block_page_server.py`, so `:3131` is reachable at the host's real
LAN address with no docker-compose changes -- `--CAPTIVE_PORTAL_DISABLED`
is an operator kill switch). A successful login (checked against
`users.password_hash` via the same `auth.verify_password()` the admin
dashboard login already uses) flips `is_authenticated` for whichever
device the request's own source IP resolves to
(`common/device_identity.py`'s new `resolve_device()`, the write-side
counterpart to that module's existing `resolve_user()`) -- DNS-tier
access only, never `bump_enabled`, `COALESCE`d so an admin's own prior
`user_id` assignment is never overwritten. Self-resolving success path,
not something this module has to handle itself: once
`controller/main.py`'s next reconcile cycle moves the device's IP into
`authenticated_v4` in the real kernel ruleset, its port-80 traffic
simply stops being redirected here at all, so the OS's own routine
re-probe reaches the real Apple/Google/Microsoft server directly and
the OS dismisses its own captive-portal UI on its own.

**Built alongside the login form, not retrofitted, per this project's
own standing security-by-design practice**: a per-source-IP rate
limiter (5 failed attempts/60s, in-memory) that blocks the NEXT attempt
outright once tripped -- including one with the actually-correct
password, closing the gap a naive failures-only counter would leave
open (use up the budget on wrong guesses, slip the right one in
unrestricted at the end) -- and `Cache-Control: no-store` on every
response, since this is per-device, per-moment login state that must
never be cached by a browser or an OS's own prober. This also let a
stale, now-corrected claim in `docs/security/overview.md` §6 be fixed
("there is none, anywhere in this codebase" was true when written,
isn't anymore).

**Known, deliberate limitation, not an oversight**: no interception for
HTTPS (tcp/443) -- matches virtually every real commercial captive
portal (there's no cert this project's own CA can present that an
ungated device already trusts, same reasoning
`block_page_server.py` already established for AdGuard's hard-deny
case), and correctly triggers the OS-native flow for the overwhelming
majority of real usage anyway, since the OS's own automatic probe uses
plain HTTP specifically for this reason. A technically determined user
who notices the redirect and never completes an HTTP request could
evade the prompt indefinitely -- tracked here as a possible future
hardening item (e.g. dropping tcp/udp 443 for `unauthenticated_v4` too)
rather than added now without verifying it doesn't also break the OS's
own captive-portal-assistant webview, which sometimes needs its own
auxiliary HTTPS requests to render correctly.

22 new tests across two new files
(`tests/test_captive_portal_server.py`, following
`tests/test_block_page_server.py`'s own real-integration-test pattern
exactly, and `tests/test_device_identity.py`, a first dedicated test
file for a previously-untested module). Also visually and interactively
verified in a live browser against `dashboard/dev_server.py`: rendered
the real login page, completed a real login through the actual form
(not just a raw `fetch()`), and confirmed `is_authenticated` flipped in
the real on-disk dev DB afterward. **Also rebuilt and redeployed the
real production `dashboard` container on the smoke-test VM** (not just
the local dev server): confirmed it starts clean with all three servers
(dashboard/block-page/captive-portal) listening, and a real `curl`
against `:3131`'s Apple/Google probe paths returns 200 with the actual
login page -- caught a real Dockerfile gap doing this that a redeploy
would otherwise have hit silently: `dashboard/Dockerfile`'s `COPY` line
names each dashboard `.py` file individually rather than a wildcard, so
the new module needed adding there explicitly or the container would
have failed to start with an `ImportError`.

**Reminder screen (the design sketch's fourth bullet): done and
verified 2026-08-31**, closing it from both directions. Kid-facing: the
captive-portal success page shows a note when the logging-in user
already has a DIFFERENT device with `bump_enabled = 1` elsewhere --
since this login only ever grants DNS-tier access, a kid whose usual
device has full SSL-Bump refinement would otherwise have no idea why
something that works there doesn't work here. Admin-facing:
`dashboard/dashboard.py`'s device-detail page now prompts a plain
`confirm()` (matching this app's own established no-framework
convention, same pattern as the group-delete button) asking whether the
CA certificate is already installed before actually checking
`bump_enabled` -- reverts to unchecked if declined. Deliberately a
client-side reminder, not a server-side gate (per the design sketch's
own wording, "reminder," not "block") -- `update_device()` still
accepts either value regardless. 4 new tests; the `confirm()` behavior
itself (not just that it's wired up) was additionally verified live in
a real browser by overriding `window.confirm` to simulate both Cancel
(checkbox correctly reverts) and OK (stays checked).

**With this, every bullet in the original 2026-08-30 design sketch
below is now built and verified**: gate a new MAC by default (Milestone
1), the kid-facing login path (Milestone 3), the admin-facing path
(Milestone 2's Bypass/Manage actions -- the design sketch's own text
offered "the same portal screen, OR a separate device with real
dashboard access"; the dashboard path was built, satisfying that
bullet), and the reminder screen (this entry). Phase 4's remaining open
question from 2026-08-30 (MAC-randomization/login-frequency tuning) was
never a build item -- it's an inherent property of how OSes rotate
private addresses, noted for awareness, not something this codebase can
control. What Phase 4 does NOT yet have, and was never in the original
sketch: a portal-side (as opposed to dashboard-side) admin quick-add
action for when an admin is physically at the gated device itself
rather than on a separate device -- a possible future nice-to-have, not
a gap in what was actually planned.

**Portal-side admin action: done and verified 2026-08-31** (user: "yes
please build the portal side admin action"), closing that one
nice-to-have. `dashboard/captive_portal_server.py`'s login page gained
a collapsed `<details>` section asking for the same admin credentials
`dashboard.py`'s HTTP-Basic login checks, offering **Bypass** (identical
effect to `/devices/bypass_login`) or **assign to a group** (clears any
prior `user_id` -- required, not just tidy, since `devices.group_id`/
`user_id` are mutually exclusive per the table's own `CHECK`
constraint -- and sets `is_authenticated = 1` directly). Shares the kid
login form's own per-IP rate limiter rather than a separate budget, the
more conservative choice given this surface grants strictly more.
Factored `verify_admin_credentials()` out into `common/auth.py`
(`dashboard.py`'s own `_check_admin_auth` now delegates to it too) so
there's exactly one admin-credential check shared by both surfaces,
not two.

**Real bug found and fixed while building this**: `common/policy_class.py`'s
`classify_device()` never consulted `bypass_login` at all -- despite
the dashboard's own hint text and this very design sketch both
describing it as exempting a device from the captive-portal gate, a
`bypass_login` device was, in reality, staying stuck in `PREAUTH`
forever, still redirected to the portal on every request. This means
Milestone 2's own dashboard Bypass button was *also* a no-op at the
network-policy level the whole time it existed -- fail-closed (the
device stayed gated rather than being wrongly exposed), so not a
security hole, but a real functional gap between what the UI claimed
and what actually happened in the kernel. Fixed: `classify_device()`
now treats `is_authenticated OR bypass_login` as sufficient for
`AUTHENTICATED` (still distinct from `ignored`/`BYPASS` -- it only
skips the login requirement, quarantine still takes precedence over
it). `controller/policy_state.py`'s own query needed a companion fix --
it never even `SELECT`ed `bypass_login` in the first place, so
`classify_device()` could not have honored it regardless of its own
logic. 3 new regression tests, including one true end-to-end proof
through `compute_desired_policy()` itself, not just the pure
`classify_device()` unit.

20 new tests total across four files
(`tests/test_policy_class.py`, `tests/test_controller_policy_state.py`,
`tests/test_auth.py`, `tests/test_captive_portal_server.py`). Also
visually and interactively verified in a live browser: opened the
collapsed admin section, entered real admin credentials, picked a real
group from the live dropdown (populated from a real DB read, not a
static list), submitted, and confirmed the device's `group_id`/
`is_authenticated` actually changed in the dev DB afterward.

**Full audit of every other `bump_enabled`/`is_authenticated`/
`bypass_login` check in the codebase, 2026-08-31** (user: "audit the
other bump_enabled and isA_uahtneitcated checks for similar gaps"),
prompted directly by the `classify_device()` bug just found. Traced
every read site (not just the writes) across Python and Go:

- **`controller/adguard_sync.py`'s `build_rules()` -- a second real
  bug, same class exactly, found and fixed the same session.** It
  selected devices to hard-deny using the raw `d.bump_enabled = 0`
  column instead of the actual derived `bump_eligible()` state (which
  also requires `AUTHENTICATED`). A device with `bump_enabled = 1` but
  not yet authenticated -- a genuinely new PREAUTH device Phase 4
  auto-creates, or one an admin pre-configured for bump ahead of its
  first login -- was excluded from AdGuard's hard-deny list (since
  `bump_enabled = 1`) while ALSO not a member of nftables' `bump_v4`
  set (not `AUTHENTICATED` yet) -- meaning its HTTPS connections to
  `mode='bump'` domains went straight to the real internet completely
  unfiltered. Worse than either a hard deny or Squid refinement: a full,
  silent bypass of the exact invariant this module exists to enforce.
  Fixed to select the same way `policy_state.py` already does (fetch
  the columns `bump_eligible()` needs, exclude only what it actually
  returns True for). 1 new regression test
  (`tests/test_controller_adguard_sync.py`).
- `controller/desired_state.py` -- checked, correct by design and
  already documented as such: interception scope (who gets ARP-spoofed
  at all) is deliberately independent of policy scope (`is_authenticated`
  plays no part), per `docs/design/phase3-technical-design.md` section 5.
- `proxy/authz_helper.py`/`sni_helper.py`/`common/squid_helper.py` --
  checked, correctly touch NONE of these columns at all. By the time a
  connection reaches Squid's intercept ports, nftables has already
  guaranteed `bump_eligible()` was true (that's the only way traffic
  gets redirected there) -- Squid re-deriving or re-checking the same
  state would be duplicated logic with its own chance to drift, not
  extra safety.
- `common/matching.py` -- checked, doesn't touch these columns at all
  (a different concern: per-user/group/device domain access lists).
- `phase3/nftables-manager`'s Go side (`dbsource`, `policy` packages) --
  checked, only ever reads the Python-precomputed `desired_policy_json`
  blob; no independent/duplicated derivation of these flags exists on
  that side to drift from the Python one.
- `dashboard/dashboard.py`'s own `pending`/Status-column display logic
  (Milestone 2) -- checked; cosmetic-only (decides what badge to show,
  never enforcement), and correctly excludes `bypass_login`/`ignored`
  devices from "awaiting login" post-fix. One real but currently
  unreachable inaccuracy noted, not fixed: it doesn't check
  `quarantined_at`, so a hypothetically quarantined-and-unauthenticated
  device would display as "awaiting login" even though `QUARANTINE`
  actually takes precedence and such a device's packets are dropped
  outright (never even reaching the portal) -- left alone since nothing
  in the dashboard can set `quarantined_at` yet (Milestone 8's own
  documented gap), so this can't currently occur.
- Every `INSERT INTO devices` site (exactly two: `dashboard.py`'s
  `add_device()` and `identity.py`'s `_create_pending_device()`) --
  checked, both set exactly the flag combination their own already-
  audited docstrings claim (schema defaults for an admin-created
  device; explicit `is_authenticated = 0` for an auto-discovered one).

No further gaps found. 552 tests total (522 passed/30 skipped on
Windows) after this pass. **VM redeploy note**: the smoke-test VM's
4pm-Eastern auto-shutdown hit mid-redeploy of this fix -- `git pull`,
image rebuild, and `docker compose up -d --force-recreate controller`
all completed and the container reported "Started" before the SSH
connection was cut, so the fix is very likely live, but the final
health/log confirmation this project's own discipline calls for was
not obtained. Confirm `/health` and re-check controller logs next time
the VM is up before treating this as fully verified live.

**2026-08-31, same day: "tighter Squid/AdGuard integration" request
surfaced a THIRD real bug, bigger than the two above, plus closed a
genuine feature gap ([GH #9](https://github.com/J1nx888/parental_proxy/issues/9)).**
User was poking at the Report page with seeded dev data and noticed two
things: no way to filter by device/group, and AdGuard traffic never
showed up at all. Investigating turned up a live bug on Squid itself,
not just an AdGuard gap:

- **The bug**: `common/device_identity.py`'s `resolve_user()` INNER
  JOINed straight from `device_bindings` to `users` through
  `devices.user_id` -- a device assigned to a GROUP (not a person)
  resolved to no identity at all, and both `proxy/authz_helper.py`
  (bump-tier) and `proxy/sni_helper.py` (splice-tier) denied it
  everything before ever reaching a domain check. Even a user-resolved
  device could never benefit from a `group_domains`/`device_domains`
  grant -- `common/matching.py`'s `group_has_domain()`/
  `device_has_domain()` existed but were never called by any
  enforcement path at all (their own docstrings admitted it). **Net
  effect: a domain assigned to a group or directly to a device did
  nothing on Squid, live, in production-ready code, until this fix.**
- **The gap**: `controller/adguard_sync.py` enforced exactly one thing
  -- hard-deny `bump`-mode domains for non-bump-eligible devices. It
  never touched `splice`-mode domains (what most devices actually use)
  at all, so the entire user/group/device/everyone content allowlist
  built on the Domains page was completely unenforced at the DNS tier.
- **The other logging gap**: `access_log` had no `device_id` column,
  and `dashboard/block_page_server.py` (AdGuard's kid-facing block
  page) wrote nothing to the DB at all, not even for the block itself.

**Fix, in four stages, one shared resolver instead of two duplicated
ones** (design planned via a written implementation plan, reviewed and
approved before any code changed, per the user's explicit "scope a
plan first" direction):

1. `common/matching.py` gained `device_domain_reason(conn, device,
   domain)` -- the single authorization check (is_global -> user ->
   group -> device) now used everywhere. `common/device_identity.py`'s
   `resolve_user()` was replaced with `resolve_user_for_device()` +
   `log_identity_fields()`, so "no user" and "no identity at all" can
   never be conflated again. Both proxy helpers rewritten to resolve
   the DEVICE first (`resolve_device()`, already existed) rather than
   jumping straight to a user. New reason `show_requires_user` for a
   crunchyroll-kind domain authorized via group/device but with no
   resolvable user (user_shows has no group/device equivalent).
2. `access_log.device_id` added (the established `_migrate()`
   ALTER-TABLE-if-missing pattern). `logging_util.log_access()` gained
   an optional `device_id` param (not part of the dedupe key).
   `block_page_server.py` now writes a real row per hit, wrapped in a
   try/except so a DB hiccup can never break the actual page a kid is
   looking at.
3. `controller/adguard_sync.py` gained `build_splice_deny_rules()` --
   same `$client=`-scoped-rule mechanism as the existing bump hard-deny,
   now also covering `mode='splice', is_global=0` domains. **Scope
   locked with the user**: only enforces domains that already have a
   row; a domain with no row at all stays default-allow at the DNS
   tier, unchanged from today -- default-deny-for-unconfigured there is
   a deliberately separate future decision.
4. Report page: the plain `<select name="user">` filter replaced with
   the same combobox widget the Domains page already uses, now
   including groups/devices (`target=user:5`/`group:2`/`device:7`,
   legacy `?user=<username>` still works). `approve_from_report()`'s
   `scope` extended from `user`/`global` to also accept `device`/
   `group`, granting `device_domains`/`group_domains` instead of
   `user_domains`. No new "Block" action needed -- revoking already
   existed via the Domains page's per-assignment delete; this just
   makes that revoke *actually take effect* on both tiers, live.

19 new tests across `tests/test_helpers_protocol.py`,
`tests/test_device_identity.py`, `tests/test_block_page_server.py`,
`tests/test_controller_adguard_sync.py`, `tests/test_dashboard.py`.
Verified live end-to-end in a browser against seeded dev data, not just
via the test suite: filtered the Report page to one device via the new
combobox (confirmed exactly one row, correct stat-strip totals),
clicked "Approve for Device 1" on a device-only denied row, and
confirmed directly in the dev DB that a `domains` row (`splice`,
`is_global=0`) and a `device_domains` grant row were both created.

**Same day, before committing: the project owner reviewed and clarified
the intended design further, changing real behavior.** Five explicit
points, restated and confirmed back before implementing:

1. AdGuard's baseline protection applies to every device/user/group
   *unless* that device is `ignored` (BYPASS) -- not `bypass_login`,
   which is a separate flag that only skips the captive-portal login
   step; a `bypass_login` device still belongs to whatever user/group
   it's assigned and is still filtered normally (confirmed against
   `classify_device()`'s own docstring, which already made this
   distinction explicit).
2. `bump_enabled` is already per-device, not per-user -- one user can
   have both a bumped and a non-bumped device. Already true, no change.
3. **The actual behavior change**: for a `bump`-mode domain, a
   bump-eligible device should ALSO have AdGuard check whether that
   specific domain is assigned to it -- not just whether the device is
   bump-eligible at all. If not assigned, AdGuard blocks it outright and
   the connection never reaches Squid.
4. A non-bump device already gets this domain-assignment check (the
   `build_splice_deny_rules()` work above) -- confirmed as intended, not
   changed.
5. Whatever's blocked for a user/device on the Domains page should be
   blocked in AdGuard too, for that same user/device -- confirmed as the
   whole point of both `build_rules()` and `build_splice_deny_rules()`.

**Implementation**: `controller/adguard_sync.py`'s `build_rules()` and
`build_splice_deny_rules()` were unified onto one shared engine,
`_build_domain_deny_rules(conn, mode, require_bump_eligible, ...)`. A
device is authorized for a domain when `matching.device_domain_reason()`
returns non-None AND (for `mode='bump'` only) `bump_eligible()` is also
true -- `is_global` on a bump domain still only ever means "assigned to
everyone," never "skip the bump-eligibility gate too" (verified: a
non-bump-eligible device is still denied a global bump domain exactly as
before this change). The engine also excludes any device
`classify_device()` returns `BYPASS` for (`ignored=1`) from every rule it
builds, on either mode, per point 1.

**Point 1's `ignored` vs `bypass_login` distinction became a fourth
request, not just a clarifying answer**: "anything that gets the
bypass_login should be added to ignore by default but give me the
ability to change that group later." Implemented in `dashboard.py`'s
`update_device()` and `bypass_login_device()`: turning `bypass_login` on
now defaults `ignored` to 1 too -- but only as a genuine default, not a
forced override. Three guards, all tested: (a) skipped entirely if the
same submission explicitly picked a user/group assignment (an explicit
choice always wins); (b) skipped if the device already has a real
`user_id`/`group_id` from before; (c) only fires on the actual
bypass_login 0->1 transition (checked against the row's current DB
value), so a later save with bypass_login already on never re-forces
`ignored` back on top of an admin's own subsequent "actually, assign it
somewhere" edit.

**Why the old `build_rules()`/`build_splice_deny_rules()` tests all kept
passing unchanged (26 of them)**: every existing test fixture uses
`is_global=True` bump domains, under which the new `device_domain_reason()`
check trivially returns `"global_domain"` for every device -- so the
only thing that ever differentiated allow/deny in those tests was
already `bump_eligible()`, exactly as before. The new behavior (a
bump-eligible device denied a *specific*, non-global, unassigned bump
domain) only shows up under `is_global=False`, which none of the
original tests exercised -- confirming the rework is a strict
extension, not a silent behavior change to anything already covered.
10 new tests added: 5 in `tests/test_controller_adguard_sync.py` -- a
bump-eligible device denied an unassigned non-global bump domain, then
allowed once assigned via `device_domains`; a non-bump-eligible device
still denied on a *global* bump domain (confirming point 3 doesn't
weaken the original invariant); an `ignored` device excluded from both
`build_rules()` and `build_splice_deny_rules()` even when it would
otherwise clearly be denied -- plus 5 in `tests/test_dashboard.py` for
the bypass_login-defaults-to-ignored behavior (the plain default,
skipped-on-explicit-assignment, and skipped-on-a-later-resave cases, for
both the quick-action route and the full edit form). 552 tests total
after this pass.

## Original design sketch (2026-08-30, not started at the time)

Force all internet through the system regardless of per-device
configuration, hijacking OS captive-portal-detection probes (Apple/
Google's well-known plain-HTTP endpoints) to trigger a login prompt for
any never-seen-before device identity — auto-associating device↔user
without manual registration, and defeating MAC-randomization as an
identity-evasion trick as a side effect (any unrecognized identity,
randomized or not, just triggers another login).

**Concrete flow, worked out 2026-08-30 (still a design, no code yet):**

- A newly-seen MAC is gated behind the portal by default, **unless**
  it's already registered as bypass/ignore or assigned to a device
  group ahead of time (e.g. a smart TV or IoT device an admin never
  wants interrupted at all).
- **Kid-facing path**: logging in with a personal account grants
  `is_authenticated` (DNS-tier protection) for that device, and nothing
  else — never `bump_enabled`. If that kid's usual device set is known
  to include a Squid-enabled one and this is a new/different device,
  show a reminder that Squid-level access needs a parent's help to set
  up (CA cert), rather than silently granting or silently failing.
- **Admin-facing path**, available at the same portal screen (or from
  a separate device with real dashboard access) for a device an admin
  is physically present for: add it straight to the bypass list, or
  assign it to a device group with its own DNS-tier rules — skipping
  the login flow entirely for devices that will never have their own
  user (the thermostat, a shared family device).
- See the "Authentication and bump-tier" section above for how this
  interacts with `bump_enabled` — the portal only ever touches
  `is_authenticated`; enabling Squid for a specific device is a
  separate, deliberate admin action taken afterward, once the CA cert
  is actually installed.

Open questions not yet resolved: the session/token design for
"authorized" state at the network layer (MAC is unreliable, raw IP
alone isn't perfectly stable either); login-frequency tuning depending
on how aggressively a given OS rotates its MAC address; a MAC allowlist
for non-interactive devices (smart TVs, voice assistants) that can't
complete a login flow; the exact UI for the admin quick-add path
described above.

## Phase 5 — Admin dashboard: PWA and control surface (begun 2026-08-31)

Responsive layout (desktop/mobile) and an installable PWA don't require
a React rewrite — achievable on the existing Flask/Jinja2 architecture,
already partly in place via Phase 1's PWA work (manifest/service worker
already shipped there; this phase is the rest of the responsive-layout
and installability work on top of that foundation). Also covers any
further admin-facing UI/control-surface work beyond what exists today
that isn't specifically about remote-access hardening (that's its own
concern — see Phase 7).

**2026-08-31: live mobile/tablet audit, one real bug found and fixed.**
Started this phase early (real-network testing is blocked, so this work
doesn't need to wait). Walked every dashboard page —
Report/Devices/device-detail/Users/Domains/Settings — live in a headless
browser at 375×812 (mobile) and 768×1024 (tablet), checking
`document.documentElement.scrollWidth` against `clientWidth` on each
(the actual test for real page-level horizontal scroll, not just "does
it look okay in a screenshot").

Found one genuine bug: `.chart-grid` (Report page's two Chart.js
canvases) overflowed the viewport at mobile width —
`scrollWidth` 404px in a 375px viewport. Root cause is the classic CSS
Grid gotcha: a grid item's `min-width` defaults to `auto`, which floors
its track at the content's *intrinsic* size — a `<canvas>` gets a real
pixel `width` attribute (its internal render resolution) independent of
whatever CSS width the grid tries to assign it, so that intrinsic size
can hold the whole row open past the viewport. Fixed by adding
`min-width: 0` to `.chart-card` and `max-width: 100%` to its `canvas`
(`dashboard/static/css/app.css`). Confirmed fixed:
`scrollWidth`/`clientWidth` both 375 after.

Everything else already held up with no changes needed: the sidebar
already collapses to an icon-only rail below 720px
(`html.sidebar-collapsed`/the `@media (max-width: 720px)` block already
in `app.css`), `.access-grid`'s 3-column picker already collapses to
one column, wide tables already scroll inside their own
`.table-scroll` wrapper instead of pushing the page, forms already
stack full-width, and the tablet width (768px, just past the 720px
breakpoint) showed the full sidebar + two-column chart grid with room
to spare. PWA installability was already fully wired from Phase 1: a
theme-color meta tag, `<link rel="manifest">`, `<link
rel="apple-touch-icon">`, and the `/sw.js` registration script are all
already in `dashboard.py`'s page shell. The Phase 4 captive-portal page
(`dashboard/captive_portal_server.py`) was already built mobile-first
(`viewport` meta tag, `max-width: 24rem; margin: 4rem auto` card
layout) — not re-verified live this pass, lower priority since it was
never a general-purpose desktop-first layout to begin with.

**Testing-harness note for next time**: this headless browser tool does
not retain HTTP Basic Auth credentials across a page's own subresource
requests (embedding `user:pass@host` in the top-level navigation URL
authenticates the document itself but CSS/JS/icon requests the page
then issues still 401 — a limitation of this specific automation setup,
not of real browsers, which cache Basic Auth per-origin and attach it
to every subsequent request in that realm). Worked around it by running
a scratch copy of `dashboard/dev_server.py` with
`dashboard._check_admin_auth` monkey-patched to always return `True`,
on a different port, never touching any tracked file. That scratch
script was left in the session scratchpad, not the repo.

## Phase 6 — YouTube channel/creator-level filtering (assessed, not started)

Same fundamental shape as the existing Crunchyroll integration: the
YouTube Data API can resolve a channel handle to a stable ID, but
network visibility into *which* video/channel a request was for still
requires SSL-Bump on the metadata/API-call domains — the Data API
doesn't remove that requirement, it just helps classify what's seen.
Actual video bytes (`googlevideo.com`) get spliced/trusted once the
metadata-layer request is already approved, mirroring how Crunchyroll's
CDN domains are handled today.

**Known risk**: Google is a heavy QUIC/HTTP-3 adopter and Squid's bump
is TCP-only — if a connection negotiates QUIC over UDP/443 instead of
falling back to TCP, this filtering is silently bypassed. Resolution
agreed: block outbound UDP/443 for bump-tier devices at the
router/firewall level to force TCP fallback, not something to solve
inside this codebase.

## Phase 7 — Remote Access Hardening (assessed, not started)

The one real-stakes item in exposing this dashboard beyond the LAN:
since it controls a child's internet access, that exposure means real
auth hardening, TLS, and likely a VPN (Tailscale/WireGuard) rather than
raw port forwarding — worth designing the session/auth model correctly
from the start rather than retrofitting later, even though it's not
needed yet.

---

## Cross-cutting: security-by-design

Security is designed in from the start on every phase above, not
retrofitted — even though this stays LAN-only for now, since external
exposure is an eventual goal (Phase 7). Concrete surfaces already
flagged: the Phase 4 login flow is a brand-new externally-reachable(-
eventually) auth surface needing real scrutiny (credential handling,
brute-force/lockout, session design); the DNS resolver added in Phase 3
is new attack surface too (cache poisoning/spoofing resistance, not just
"does it block the right domains"); the interception daemon's privilege
split (a narrow `CAP_NET_RAW`-only worker, separate from the
unprivileged controller and the `CAP_NET_ADMIN`-scoped nftables manager)
is itself a security decision, not just an implementation detail.
