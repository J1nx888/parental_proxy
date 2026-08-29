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
| 3 | Network-level interception (the actual Bark Home replacement mechanism) | 🔶 Architecture locked, implementation not started |
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

### `devices.is_authenticated` semantics

This flag does **not** control whether a device is ARP-spoofed — every
in-scope device stays intercepted regardless. It only selects policy
once intercepted:

- `authenticated_v4` — normal Squid/AdGuard policy.
- `unauthenticated_v4` — DNS to AdGuard, HTTP redirected to the future
  Phase 4 login portal, HTTPS handled by a deliberate pre-auth policy.
- `bypass_v4` — infrastructure that must never be touched: Orbi nodes,
  the interception box itself, manually-exempted devices.
- `quarantine_v4` — an optional, explicitly operator-triggered isolation
  state.

### Fail-open engineering (a correction to an earlier assumption)

Linux neighbor-cache entries are a state machine, not a fixed TTL — a
stale mapping to a dead interception box can blackhole a client's
traffic for a real, bounded period after an *ungraceful* crash, even
though a *graceful* shutdown can proactively send corrective ARP
replies. This must be engineered before any testing against the real
home network, not added afterward:

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
- [ ] **4. Identity model** — `device_bindings`, outbox events, MAC/IP
      conflict handling.
- [ ] **5. `nftables` integration** — dedicated table, named policy
      sets, atomic apply/rollback.
- [ ] **6. Service health** — Squid/AdGuard/controller readiness gates,
      systemd watchdog + restart limits.
- [ ] **7. Authentication workflow** — toggling
      `devices.is_authenticated` updates policy without restarting
      spoofing.
- [ ] **8. Future-portal seam** — implement the `PolicyClass` enum
      (`AUTHENTICATED` / `PREAUTH` / `BYPASS` / `QUARANTINE`) now, even
      though only the first two are used until Phase 4.
- [ ] **9. Fault campaign** — signals, OOM kill, NIC down/up, gateway
      reboot, DB lock, malformed IPC, partial `nftables` failure.
- [ ] **10. Soak test** — 7–14 days of mixed real household load,
      roaming, sleep/wake, with memory/FD/CPU trend monitoring.

Nothing under Phase 3 has been built yet — this section is the plan, not
a status report on code that exists.

---

## Phase 4 — Captive-portal forced enrollment (design sketch, not started)

Force all internet through the system regardless of per-device
configuration, hijacking OS captive-portal-detection probes (Apple/
Google's well-known plain-HTTP endpoints) to trigger a login prompt for
any never-seen-before device identity — auto-associating device↔user
without manual registration, and defeating MAC-randomization as an
identity-evasion trick as a side effect (any unrecognized identity,
randomized or not, just triggers another login).

Open questions not yet resolved: the session/token design for
"authorized" state at the network layer (MAC is unreliable, raw IP
alone isn't perfectly stable either); login-frequency tuning depending
on how aggressively a given OS rotates its MAC address; a MAC allowlist
for non-interactive devices (smart TVs, voice assistants) that can't
complete a login flow.

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
