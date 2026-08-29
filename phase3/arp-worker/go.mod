module github.com/J1nx888/parental_proxy/phase3/arp-worker

go 1.25.0

// Resolved via `go mod tidy` on the smoke-test VM, 2026-08-29. The
// `go 1.25.0` directive above is golang.org/x/sys's own minimum, not
// something this project needed -- `go mod tidy` picked it up
// transitively and Go's toolchain auto-download (GOTOOLCHAIN=auto)
// fetched go1.26.7 locally to satisfy it.

require (
	github.com/coreos/go-systemd/v22 v22.7.0
	github.com/mdlayher/arp v0.0.0-20260528070854-93566ba168e9
	golang.org/x/sys v0.47.0
)

require (
	github.com/josharian/native v1.1.0 // indirect
	github.com/mdlayher/ethernet v0.0.0-20220221185849-529eae5b6118 // indirect
	github.com/mdlayher/packet v1.1.2 // indirect
	github.com/mdlayher/socket v0.4.1 // indirect
	golang.org/x/net v0.38.0 // indirect
	golang.org/x/sync v0.1.0 // indirect
)
