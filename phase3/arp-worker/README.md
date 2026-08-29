# pp-arp-worker (Milestone 2 scaffold)

Status: **written, not yet built or run.** This dev environment has no
Go toolchain, so nothing here has been compiled, `go vet`'d, or tested
— see the header notes in `internal/arpio/mdlayher_adapter.go` and
`internal/ipc/peercred_unix.go` for the two files most likely to need a
fix once a real build is attempted. Everything else (`internal/worker`,
`internal/ipc`'s protocol/dispatch) has no external dependencies and no
OS-specific syscalls, so it's the part most likely to already be
correct.

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

## First build, on the smoke-test VM (once it's reachable)

```bash
cd phase3/arp-worker
go mod tidy      # fetches mdlayher/arp, golang.org/x/sys, coreos/go-systemd; writes go.sum
go build ./...
go vet ./...
go test ./...    # internal/worker and internal/ipc tests need no root/NIC/CAP_NET_RAW
```

`go build` will very likely surface a handful of API mismatches in
`internal/arpio/mdlayher_adapter.go` and `internal/ipc/peercred_unix.go`
— those two files were written from memory of the packages' documented
shape, not against a fetched copy, precisely because no toolchain was
available while writing them. Everything under `internal/worker` and
`internal/ipc/{protocol,dispatch}.go` has no third-party dependency and
should build clean.

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
