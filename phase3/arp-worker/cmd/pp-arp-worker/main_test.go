package main

import (
	"net"
	"testing"

	"github.com/J1nx888/parental_proxy/phase3/arp-worker/internal/ipc"
	"github.com/J1nx888/parental_proxy/phase3/arp-worker/internal/worker"
)

// fakeSender is a minimal worker.ARPSender double -- no real socket or
// CAP_NET_RAW needed, just enough for a *worker.Worker to exist and
// ApplyGeneration to run without erroring, so HandleReplaceTargets's
// OWN logic (target validation, target count, failure reporting) can
// be tested in isolation.
type fakeSender struct{}

func (fakeSender) Reply(net.IP, net.HardwareAddr, net.IP, net.HardwareAddr) error { return nil }
func (fakeSender) Resolve(net.IP) (net.HardwareAddr, error)                       { return nil, nil }
func (fakeSender) Close() error                                                   { return nil }

func newTestHandler(t *testing.T) *controllerHandler {
	t.Helper()
	w := worker.New(fakeSender{}, net.HardwareAddr{0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0x99}, worker.DefaultConfig())
	_, subnet, err := net.ParseCIDR("192.168.1.50/24")
	if err != nil {
		t.Fatalf("ParseCIDR: %v", err)
	}
	h := &controllerHandler{
		worker: w,
		selfIP: net.ParseIP("192.168.1.50"),
		subnet: subnet,
	}
	h.lease = worker.NewLeaseMonitor(worker.DefaultConfig().Interval, 5, h.onLeaseExpired)
	t.Cleanup(h.lease.Stop)
	return h
}

// TestHandleReplaceTargets_RejectsTheGateway is a regression test for a
// real gap found by code review (2026-09-02): worker.ValidateTargets
// existed but HandleReplaceTargets never called it, so a
// controller-side bug sending the gateway's own IP as a "poison this"
// target would have been applied unquestioningly.
func TestHandleReplaceTargets_RejectsTheGateway(t *testing.T) {
	h := newTestHandler(t)
	reply := h.HandleReplaceTargets(ipc.ReplaceTargets{
		V: ipc.ProtocolVersion, Op: "replace_targets", Generation: 1,
		Gateway: ipc.Target{IP: "192.168.1.1", MAC: "aa:bb:cc:dd:ee:01"},
		Targets: []ipc.Target{
			{IP: "192.168.1.1", MAC: "aa:bb:cc:dd:ee:01"}, // the gateway itself -- must be rejected
			{IP: "192.168.1.21", MAC: "aa:bb:cc:dd:ee:22"}, // a normal target -- must be accepted
		},
	})
	ack, ok := reply[0].(ipc.GenerationApplied)
	if !ok {
		t.Fatalf("expected a GenerationApplied reply, got %T", reply[0])
	}
	if ack.TargetCount != 1 {
		t.Fatalf("expected exactly 1 accepted target (the gateway rejected), got %d", ack.TargetCount)
	}
	found := false
	for _, f := range ack.ResolutionFailures {
		if f == "192.168.1.1" {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected the gateway's IP in ResolutionFailures, got %v", ack.ResolutionFailures)
	}
}

// TestHandleReplaceTargets_RejectsSelfAndBroadcast covers the other two
// checks achievable without a wire-protocol change (self and subnet
// broadcast -- see HandleReplaceTargets's own comment on why bypass_v4
// isn't checked here yet).
func TestHandleReplaceTargets_RejectsSelfAndBroadcast(t *testing.T) {
	h := newTestHandler(t)
	reply := h.HandleReplaceTargets(ipc.ReplaceTargets{
		V: ipc.ProtocolVersion, Op: "replace_targets", Generation: 1,
		Gateway: ipc.Target{IP: "192.168.1.1", MAC: "aa:bb:cc:dd:ee:01"},
		Targets: []ipc.Target{
			{IP: "192.168.1.50", MAC: "aa:bb:cc:dd:ee:50"},  // this worker's own IP
			{IP: "192.168.1.255", MAC: "aa:bb:cc:dd:ee:ff"}, // the /24's broadcast address
			{IP: "192.168.1.21", MAC: "aa:bb:cc:dd:ee:22"},  // a normal target
		},
	})
	ack, ok := reply[0].(ipc.GenerationApplied)
	if !ok {
		t.Fatalf("expected a GenerationApplied reply, got %T", reply[0])
	}
	if ack.TargetCount != 1 {
		t.Fatalf("expected exactly 1 accepted target (self and broadcast rejected), got %d", ack.TargetCount)
	}
}

// TestHandleReplaceTargets_AcceptsOrdinaryTargets is the negative case:
// confirm the new validation step doesn't reject anything it shouldn't.
func TestHandleReplaceTargets_AcceptsOrdinaryTargets(t *testing.T) {
	h := newTestHandler(t)
	reply := h.HandleReplaceTargets(ipc.ReplaceTargets{
		V: ipc.ProtocolVersion, Op: "replace_targets", Generation: 1,
		Gateway: ipc.Target{IP: "192.168.1.1", MAC: "aa:bb:cc:dd:ee:01"},
		Targets: []ipc.Target{
			{IP: "192.168.1.21", MAC: "aa:bb:cc:dd:ee:22"},
			{IP: "192.168.1.22", MAC: "aa:bb:cc:dd:ee:23"},
		},
	})
	ack, ok := reply[0].(ipc.GenerationApplied)
	if !ok {
		t.Fatalf("expected a GenerationApplied reply, got %T", reply[0])
	}
	if ack.TargetCount != 2 {
		t.Fatalf("expected both ordinary targets accepted, got %d", ack.TargetCount)
	}
	if len(ack.ResolutionFailures) != 0 {
		t.Fatalf("expected no failures for ordinary targets, got %v", ack.ResolutionFailures)
	}
}
