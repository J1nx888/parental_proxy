# pp-arp-worker (Milestone 2 scaffold)

Status: **builds, vets, and passes its full test suite** on the
smoke-test VM as of 2026-08-29 (Ubuntu 24.04, Go 1.26.7 auto-toolchain,
`go build ./...`, `go vet ./...`, `go test ./... -count=10` all clean —
`-race` isn't available there, no C compiler on that sandboxed account).
One real API mismatch was found and fixed during that first build:
`github.com/mdlayher/arp`'s current version uses `netip.Addr`, not
`net.IP`, in `Client.Resolve` and `arp.NewPacket` — `internal/arpio/mdlayher_adapter.go`
now converts at that one boundary; the rest of the codebase still uses
`net.IP` throughout, matching the stdlib conventions the rest of this
project already follows. `peercred_unix.go`'s `SO_PEERCRED` handling
built and vetted clean on the first try.

Not yet done: running this against a real interface (needs
`CAP_NET_RAW`, deliberately never granted to this scaffold — see below),
and wiring it into an actual controller process, which doesn't exist
yet (Milestone 3).

See [`../../docs/design/phase3-technical-design.md`](../../docs/design/phase3-technical-design.md)
for the design this implements, and
[`../../RoadMap.md`](../../RoadMap.md) for how Milestone 2 fits the
overall plan.

## Layout

```
internal/worker/   Core scheduling logic: generations, corrective ARP
                    restoration, the lease/heartbeat state machine,
                    startup safety checks. No OS dependency at all --
                    ARPSender is an interface, satisfied in tests by a
                    fake that just records calls. This is where the
                    actually-tricky logic lives and where the unit
                    tests (worker_test.go, lease_test.go, safety_test.go)
                    already exist.
internal/ipc/      The controller<->worker wire protocol: message
                    structs (protocol.go), routing (dispatch.go, has
                    its own unit tests with no socket needed), and the
                    real Unix-socket server with SO_PEERCRED enforcement
                    (server.go + peercred_unix.go, Linux-only).
internal/arpio/    Thin adapter wiring github.com/mdlayher/arp to
                    worker.ARPSender. The one file most likely to need
                    hand-fixing against the real package API.
cmd/pp-arp-worker/ main.go: flag parsing, wiring everything together,
                    systemd sd_notify/watchdog integration, signal
                    handling for graceful (corrective-ARP-sending)
                    shutdown.
```

## Building it yourself

```bash
cd phase3/arp-worker
go build ./...
go vet ./...
go test ./...    # internal/worker and internal/ipc tests need no root/NIC/CAP_NET_RAW
```

`go.mod`/`go.sum` are already resolved and committed, so `go mod tidy`
isn't needed unless you're changing dependencies. If your local Go
toolchain is older than the `go` directive in `go.mod` requires,
`GOTOOLCHAIN=auto` (the default) will fetch a newer one automatically.

## What's deliberately NOT here yet

- `worker.ValidateTargets` (gateway/self/broadcast/multicast/bypass
  rejection) is implemented and unit-tested, but **not yet wired into**
  `cmd/pp-arp-worker/main.go`'s `HandleReplaceTargets` — see the TODO
  there. It needs the subnet and bypass-set on the wire, which isn't in
  the `replace_targets` schema yet; that's an open design question, not
  an oversight.
- Sustained send-failure reporting into a `fault` IPC message (currently
  errors from `ARPSender.Reply` are silently dropped per-call — see the
  TODO in `internal/worker/worker.go`'s `sendGratuitousReply`).
- Real interval/lease/corrective-repeat constants — `DefaultConfig()`
  values are explicit placeholders (RoadMap.md section 8 flags these as
  needing real numbers from the soak-test milestone, not a guess).
- Actually running this against a real interface — needs `CAP_NET_RAW`,
  which this scaffold has never been given (correctly: it should only
  ever run in the smoke-test VM sandbox until it's proven safe, per the
  project's own testing discipline).
