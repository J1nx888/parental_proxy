module github.com/J1nx888/parental_proxy/phase3/arp-worker

go 1.22

// No require block yet -- dependencies are added by running `go mod
// tidy` on a machine with a Go toolchain and network access (this dev
// sandbox has neither, see ../../docs/design/phase3-technical-design.md).
// Expected dependencies once that's run: github.com/mdlayher/arp,
// github.com/mdlayher/ethernet, golang.org/x/sys, and
// github.com/coreos/go-systemd/v22.
