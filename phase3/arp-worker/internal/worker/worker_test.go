package worker

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"
)

// fakeSender is a test double for ARPSender that just records every
// Reply call -- it lets the scheduling/switching logic in worker.go be
// verified with no real NIC, no CAP_NET_RAW, and no OS dependency.
type fakeSender struct {
	mu    sync.Mutex
	calls []replyCall
}

type replyCall struct {
	senderIP  net.IP
	senderMAC net.HardwareAddr
	dstIP     net.IP
	dstMAC    net.HardwareAddr
}

func (f *fakeSender) Reply(senderIP net.IP, senderMAC net.HardwareAddr, dstIP net.IP, dstMAC net.HardwareAddr) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, replyCall{senderIP, senderMAC, dstIP, dstMAC})
	return nil
}

func (f *fakeSender) Resolve(ip net.IP) (net.HardwareAddr, error) {
	return mustMAC("02:00:00:00:ff:ff"), nil
}

func (f *fakeSender) Close() error { return nil }

func (f *fakeSender) snapshot() []replyCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]replyCall, len(f.calls))
	copy(out, f.calls)
	return out
}

func (f *fakeSender) reset() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = nil
}

func mustMAC(s string) net.HardwareAddr {
	m, err := net.ParseMAC(s)
	if err != nil {
		panic(err)
	}
	return m
}

func TestApplyGeneration_PoisonsWithOwnMAC(t *testing.T) {
	fs := &fakeSender{}
	selfMAC := mustMAC("02:00:00:00:00:01")
	w := New(fs, selfMAC, Config{Interval: 10 * time.Millisecond, CorrectiveRepeats: 1, CorrectiveSpacing: time.Millisecond})

	gw := Target{IP: net.ParseIP("192.168.1.1"), MAC: mustMAC("02:00:00:00:00:02")}
	victim := Target{IP: net.ParseIP("192.168.1.21"), MAC: mustMAC("02:00:00:00:00:03")}
	gen := Generation{ID: 1, Gateway: gw, Targets: []Target{victim}, FullDuplex: true}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.ApplyGeneration(ctx, gen)
	time.Sleep(35 * time.Millisecond) // a few ticks

	calls := fs.snapshot()
	if len(calls) == 0 {
		t.Fatal("expected at least one poisoning ARP reply, got none")
	}
	for _, c := range calls {
		if c.senderMAC.String() != selfMAC.String() {
			t.Errorf("poisoning reply sender MAC = %s, want the worker's own MAC %s (never a real owner's during poisoning)", c.senderMAC, selfMAC)
		}
	}

	w.Shutdown()
	after := fs.snapshot()
	if len(after) <= len(calls) {
		t.Fatal("expected Shutdown to send at least one corrective ARP")
	}
	last := after[len(after)-1]
	if last.senderMAC.String() != gw.MAC.String() && last.senderMAC.String() != victim.MAC.String() {
		t.Errorf("corrective reply sender MAC = %s, want a real owner MAC (gateway %s or victim %s)", last.senderMAC, gw.MAC, victim.MAC)
	}
}

func TestApplyGeneration_OverlappingTargetStaysUninterrupted(t *testing.T) {
	fs := &fakeSender{}
	selfMAC := mustMAC("02:00:00:00:00:01")
	w := New(fs, selfMAC, Config{Interval: 5 * time.Millisecond, CorrectiveRepeats: 1, CorrectiveSpacing: time.Millisecond})

	gw := Target{IP: net.ParseIP("192.168.1.1"), MAC: mustMAC("02:00:00:00:00:02")}
	stays := Target{IP: net.ParseIP("192.168.1.21"), MAC: mustMAC("02:00:00:00:00:03")}
	leaves := Target{IP: net.ParseIP("192.168.1.22"), MAC: mustMAC("02:00:00:00:00:04")}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	w.ApplyGeneration(ctx, Generation{ID: 1, Gateway: gw, Targets: []Target{stays, leaves}})
	time.Sleep(20 * time.Millisecond)
	fs.reset() // scope the following assertions to just the generation switch

	w.ApplyGeneration(ctx, Generation{ID: 2, Gateway: gw, Targets: []Target{stays}})
	time.Sleep(20 * time.Millisecond)

	for _, c := range fs.snapshot() {
		if c.dstIP.Equal(stays.IP) && c.senderMAC.String() != selfMAC.String() {
			t.Errorf("target present in both generations received a non-poisoning reply during the switch: %+v -- this is exactly the race restorationSet() exists to prevent", c)
		}
	}

	w.Shutdown()
}

func TestApplyGeneration_LeavingTargetGetsCorrective(t *testing.T) {
	fs := &fakeSender{}
	selfMAC := mustMAC("02:00:00:00:00:01")
	w := New(fs, selfMAC, Config{Interval: 5 * time.Millisecond, CorrectiveRepeats: 1, CorrectiveSpacing: time.Millisecond})

	gw := Target{IP: net.ParseIP("192.168.1.1"), MAC: mustMAC("02:00:00:00:00:02")}
	leaves := Target{IP: net.ParseIP("192.168.1.22"), MAC: mustMAC("02:00:00:00:00:04")}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	w.ApplyGeneration(ctx, Generation{ID: 1, Gateway: gw, Targets: []Target{leaves}})
	time.Sleep(20 * time.Millisecond)
	fs.reset()

	// New generation drops "leaves" entirely.
	w.ApplyGeneration(ctx, Generation{ID: 2, Gateway: gw, Targets: nil})
	time.Sleep(10 * time.Millisecond)

	found := false
	for _, c := range fs.snapshot() {
		if c.dstIP.Equal(leaves.IP) && c.senderMAC.String() == gw.MAC.String() {
			found = true
		}
	}
	if !found {
		t.Error("expected a corrective (real gateway MAC) reply sent to the target that left scope, got none")
	}

	w.Shutdown()
}
