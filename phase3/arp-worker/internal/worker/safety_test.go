package worker

import (
	"net"
	"testing"
)

func TestValidateTargets_RejectsGatewaySelfBroadcastMulticastBypass(t *testing.T) {
	selfIP := net.ParseIP("192.168.1.250")
	gw := Target{IP: net.ParseIP("192.168.1.1"), MAC: mustMAC("02:00:00:00:00:02")}
	_, subnet, err := net.ParseCIDR("192.168.1.0/24")
	if err != nil {
		t.Fatal(err)
	}
	bypass := map[string]bool{"192.168.1.5": true}

	legit := net.ParseIP("192.168.1.21")
	candidates := []Target{
		{IP: gw.IP, MAC: gw.MAC},
		{IP: selfIP, MAC: mustMAC("02:00:00:00:00:01")},
		{IP: net.ParseIP("192.168.1.255"), MAC: mustMAC("02:00:00:00:00:03")},
		{IP: net.ParseIP("224.0.0.1"), MAC: mustMAC("02:00:00:00:00:04")},
		{IP: net.ParseIP("192.168.1.5"), MAC: mustMAC("02:00:00:00:00:05")},
		{IP: legit, MAC: mustMAC("02:00:00:00:00:06")},
	}

	accepted, rejected := ValidateTargets(selfIP, gw, subnet, bypass, candidates)

	if len(accepted) != 1 || !accepted[0].IP.Equal(legit) {
		t.Fatalf("expected exactly the one legitimate target accepted, got %+v", accepted)
	}
	if len(rejected) != 5 {
		t.Fatalf("expected 5 rejections (gateway, self, broadcast, multicast, bypass), got %d: %+v", len(rejected), rejected)
	}
}

func TestBroadcastAddr(t *testing.T) {
	_, subnet, err := net.ParseCIDR("192.168.1.0/24")
	if err != nil {
		t.Fatal(err)
	}
	got := broadcastAddr(subnet)
	want := net.ParseIP("192.168.1.255").To4()
	if got == nil || !got.Equal(want) {
		t.Fatalf("broadcastAddr(%v) = %v, want %v", subnet, got, want)
	}
}
