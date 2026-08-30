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
| 5 | YouTube channel/creator-level filtering | ⬜ Assessed, not started |
| 6 | Admin control surface: responsive/PWA, eventual remote access | ⬜ Not started |

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
entities grows past a handful.

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

      Not yet done: a friendly landing page for the blocked case
      (mirroring the existing `/blocked` page pattern) — right now a
      hard-denied bump-mode domain on a non-bump device just gets
      `0.0.0.0`/a generic browser connection error, no explanation.
      `controller/main.py` also still has no Dockerfile/compose service
      of its own (a pre-existing gap, unrelated to this item), so the
      sync loop currently has to be run by hand with credentials
      matching whatever's in `ADGUARD_USERNAME`/`ADGUARD_PASSWORD`.
- [ ] **Captive portal (Phase 4) — not started**, but now has concrete
      shape from this session's discussion: gate any newly-seen MAC
      not already registered as bypass/ignore; a kid-facing login that
      grants `is_authenticated` (DNS-tier) only, never `bump_enabled`;
      an admin-facing quick-add path at first sight of a new device
      (add to bypass, assign to a group, or full dashboard access from
      another device); and a reminder screen for an account that's
      meant to have both DNS and Squid but hasn't had the one-time CA
      cert install done yet. Recommendation, not yet confirmed: an
      admin should only flip `bump_enabled` *after* confirming the CA
      cert is actually installed, so there's no window where a device
      is bump-enabled but showing confusing certificate warnings
      instead of a clean "ask a parent" experience.

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

- [ ] **1. Topology probe** — passive discovery, full Orbi attachment
      matrix, PCAP corpus.
- [ ] **2. ARP worker MVP** — static targets, half-duplex, corrective
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
- [ ] **3. Controller** — versioned Unix-socket IPC, generations,
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
      against its real, independently-confirmed MAC); the worker's own
      lease monitor detected the controller's death on its own and
      entered repair-only mode without being told to; a SIGTERM to the
      controller correctly propagated a real "shutdown" IPC message
      that made the worker send corrective ARPs, restoring the real
      gateway's MAC in the victim's cache; `interception_runtime`'s
      health columns updated correctly throughout. Found and fixed one
      real bug along the way: `-controller-uid=0` (a legitimate UID —
      root) was being rejected as "not provided," since the flag used
      0 as both its zero-value default and its required-check sentinel.
- [ ] **4. Identity model** — `device_bindings`, outbox events, MAC/IP
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
      Still unbuilt: the higher-precedence live rtnetlink-event listener
      (needs real netlink socket programming, e.g. `pyroute2` —
      deliberately not rushed alongside this), AdGuard query-log
      correlation, and active ARP scanning — the other three sources in
      the design doc's precedence order. The snapshot loop's own
      interval is the freshness bound now, rather than "never," but it's
      still not sub-second like a live listener would be.
- [ ] **5. `nftables` integration** — dedicated table, named policy
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
- [ ] **6. Service health** — Squid/AdGuard/controller readiness gates,
      systemd watchdog + restart limits. **Done and verified 2026-08-29**:
      `common/sdnotify.py` (stdlib-only systemd sd_notify client,
      READY=1/WATCHDOG=1) wired into the controller's heartbeat pacer;
      `controller/health.py` writes `interception_runtime`'s
      `mode`/`last_healthy_at`/`fail_open_reason` — the first real use
      of that table since Milestone 4 added it. A failed reconcile
      cycle is now logged and reported as `fail_open` rather than
      crashing the process. Squid/AdGuard readiness *gates* specifically
      (blocking startup until those services answer) aren't built —
      AdGuard Home itself isn't integrated into this repo yet — but the
      health-reporting mechanism those gates would feed into now exists
      and is tested.
- [ ] **7. Authentication workflow** — toggling
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
- [ ] **8. Future-portal seam** — implement the `PolicyClass` enum
      (`AUTHENTICATED` / `PREAUTH` / `BYPASS` / `QUARANTINE`) now, even
      though only the first two are used until Phase 4. **Done
      2026-08-29**: `common/policy_class.py`'s `PolicyClass` enum +
      `classify_device()`, precedence `bypass > quarantine >
      authenticated/preauth` matching the nftables chain's own
      evaluation order. `devices.quarantined_at` added (nullable,
      nothing sets it yet — no dashboard control exists to trigger
      quarantine; that's future, user-facing work, not built here).
- [ ] **9. Fault campaign** — signals, OOM kill, NIC down/up, gateway
      reboot, DB lock, malformed IPC, partial `nftables` failure.
      **Partially done 2026-08-29 — the subset testable without real
      network hardware or destructive host access**: malformed IPC
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
      calls from 40 threads. **Not attempted, needs real hardware or a
      network-namespace harness this session didn't build**: NIC
      down/up, gateway reboot, OOM kill under real load.
- [ ] **10. Soak test** — 7–14 days of mixed real household load,
      roaming, sleep/wake, with memory/FD/CPU trend monitoring. **Not
      startable autonomously** — needs the real household network
      running this stack for real, over real time, which is squarely
      the project owner's call on when to begin (deploying an
      ARP-spoofing daemon to the live LAN is exactly the kind of step
      this project's own testing discipline says shouldn't happen
      without them directly involved). Everything upstream of this
      (Milestones 1–9) is ready to support a soak test whenever that's
      wanted; nothing else can be done to move Milestone 10 forward
      before then.

Milestones 1–9 above (all but the soak test) have real, tested — several
functionally verified against real nftables/real sockets/real subprocess
behavior, including the controller and nftables-manager coordinating
live through the shared DB (Milestone 7's end-to-end proof) — work
behind them as of 2026-08-29. What's NOT built: a full discovery daemon
(only the periodic snapshot source exists so far, not wired into a
running loop, and not the higher-precedence live rtnetlink listener), a
dashboard "interception health" view reading the tables above, and
running any of this against a real network interface (`CAP_NET_RAW`/
`CAP_NET_ADMIN` deliberately withheld from every sandboxed test account
used so far — nothing here has ever run outside a disposable VM or
container).

---

## Phase 4 — Captive-portal forced enrollment (design sketch, not started)

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

## Phase 5 — YouTube channel/creator-level filtering (assessed, not started)

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

## Phase 6 — Admin control surface (assessed, not started)

Responsive layout (desktop/mobile) and an installable PWA don't require
a React rewrite — achievable on the existing Flask/Jinja2 architecture,
already partly in place via Phase 1's PWA work. The one real-stakes item
is eventual remote access: since this dashboard controls a child's
internet access, exposing it beyond the LAN means real auth hardening,
TLS, and likely a VPN (Tailscale/WireGuard) rather than raw port
forwarding — worth designing the session/auth model correctly from the
start rather than retrofitting later, even though it's not needed yet.

---

## Cross-cutting: security-by-design

Security is designed in from the start on every phase above, not
retrofitted — even though this stays LAN-only for now, since external
exposure is an eventual goal (Phase 6). Concrete surfaces already
flagged: the Phase 4 login flow is a brand-new externally-reachable(-
eventually) auth surface needing real scrutiny (credential handling,
brute-force/lockout, session design); the DNS resolver added in Phase 3
is new attack surface too (cache poisoning/spoofing resistance, not just
"does it block the right domains"); the interception daemon's privilege
split (a narrow `CAP_NET_RAW`-only worker, separate from the
unprivileged controller and the `CAP_NET_ADMIN`-scoped nftables manager)
is itself a security decision, not just an implementation detail.
