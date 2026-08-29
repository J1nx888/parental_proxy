# pp-nftables-manager (Milestone 5 scaffold)

Status: **builds, vets, passes its unit tests, AND verified against a
real kernel nftables instance** — the strongest verification any Phase
3 component has gotten so far, since this is the one piece testable
without any special hardware: a plain Docker container with
`--cap-add=NET_ADMIN` (no host root, no sudo) gets its own nftables
state in its own network namespace, so the actual kernel behavior can
be exercised safely.

See [`../../docs/design/phase3-technical-design.md`](../../docs/design/phase3-technical-design.md)
section 5 for the design this implements, and
[`../../RoadMap.md`](../../RoadMap.md) for how Milestone 5 fits the
overall plan.

## Layout

```
internal/policy/  Pure logic, no nftables dependency at all:
                   DesiredPolicy/ActualPolicy types, ResolveConflicts
                   (an IP requested in more than one of the four policy
                   sets keeps only its highest-priority one --
                   bypass > authenticated > unauthenticated >
                   quarantine, matching the prerouting chain's own
                   evaluation order), and Reconcile (per-set add/remove
                   diff, only sets that actually changed appear in the
                   output). Fully unit tested.
internal/nft/     Adapter wiring sigs.k8s.io/knftables to internal/policy
                   -- the one piece that needs CAP_NET_ADMIN.
                   EnsureBaseline creates the table/sets/chain from the
                   design skeleton; ReadActual reads live set
                   membership; ApplyDiffs applies every set's changes
                   in ONE atomic knftables transaction.
cmd/pp-nftables-manager/  Bootstraps the baseline and exits -- no
                   reconciliation loop or desired-state input wired up
                   yet (see the binary's own -h / header comment).
```

## Verifying it yourself

```bash
cd phase3/nftables-manager
go build ./... && go vet ./... && go test ./...   # no CAP_NET_ADMIN needed for any of this

# Real functional check against actual nftables, no host root required:
go build -o /tmp/pp-nftables-manager ./cmd/pp-nftables-manager
docker run --rm --cap-add=NET_ADMIN \
  -v /tmp/pp-nftables-manager:/pp-nftables-manager:ro ubuntu:24.04 \
  bash -c 'apt-get update -qq && apt-get install -y -qq nftables >/dev/null 2>&1 &&
           /pp-nftables-manager && nft list table inet parental_proxy'
```

That last command is exactly what was run to verify this module on
2026-08-29 (Ubuntu 24.04 container, real `nft` binary, no mocking) —
the resulting ruleset matched the design skeleton exactly: four sets,
the `bypass_v4` short-circuit `return`, and every redirect rule with
the right ports. A follow-up scratch program (not checked in — see the
commit that added this file for its contents) additionally verified
`ApplyDiffs`/`ReadActual` end to end: adding elements, an idempotent
re-reconcile against unchanged desired state producing an empty diff
(proving the "idempotent reconciliation" requirement holds against a
*real* kernel ruleset, not just an in-memory fake), and an incremental
add+remove in one atomic transaction.

## What's deliberately NOT here yet

- No reconciliation loop or desired-state input — `cmd/pp-nftables-manager`
  only bootstraps and exits. A real deployment needs this driven by
  something analogous to `controller/desired_state.py`, translating
  `devices.is_authenticated` (and the still-undecided pre-auth HTTPS
  policy — see the design doc's "what this document does not decide")
  into `policy.DesiredPolicy`.
- `EnsureBaseline` is only safe to call once against a *fresh* table —
  calling it again against a table that already has the baseline rules
  would duplicate them (nftables rules aren't deduplicated by content
  the way set elements are). See the TODO in
  `internal/nft/knftables_adapter.go`.
- Whether this ships as its own binary/systemd unit or gets folded into
  the Python controller process is still open (see the design doc's
  closing section) — this scaffold assumes "own binary" since Go is
  what `knftables` is written for, but that's not locked in.
