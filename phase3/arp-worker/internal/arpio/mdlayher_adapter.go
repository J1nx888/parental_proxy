//go:build linux

// Package arpio adapts github.com/mdlayher/arp to the worker.ARPSender
// interface (see internal/worker/types.go). This is the one piece of
// the whole project that actually needs CAP_NET_RAW.
//
// NOT VERIFIED AGAINST A REAL BUILD -- this dev environment has no Go
// toolchain (see docs/design/phase3-technical-design.md's header
// note). Before the first real build on the smoke-test VM:
//
//	go get github.com/mdlayher/arp@latest github.com/mdlayher/ethernet@latest
//	go build ./...
//
// and fix any API mismatches in this file. mdlayher/arp's exact method
// set/signatures below are written from memory of the package's
// documented shape, not checked against a fetched copy -- this file is
// intentionally small and isolated so that fixing it doesn't touch any
// of the unit-tested logic in internal/worker or internal/ipc.
package arpio

import (
	"net"

	"github.com/mdlayher/arp"
)

// Client wraps *arp.Client to satisfy worker.ARPSender.
type Client struct {
	c *arp.Client
}

// Dial opens a raw-socket ARP client bound to ifi. Requires
// CAP_NET_RAW (or root) -- per RoadMap.md's least-privilege design,
// this should be the only capability the arp-worker binary is ever
// granted (see the systemd unit sketch in the design doc).
func Dial(ifi *net.Interface) (*Client, error) {
	c, err := arp.Dial(ifi)
	if err != nil {
		return nil, err
	}
	return &Client{c: c}, nil
}

// Close releases the underlying raw socket.
func (cl *Client) Close() error {
	return cl.c.Close()
}

// Resolve performs a genuine ARP request/reply exchange -- never
// served from a cache -- to learn ip's real hardware address. Used
// only by worker.ResolveGateway during startup safety checks.
func (cl *Client) Resolve(ip net.IP) (net.HardwareAddr, error) {
	return cl.c.Resolve(ip)
}

// Reply sends an ARP reply claiming senderIP is at senderMAC,
// addressed on the wire directly to dstMAC (unicast rather than
// broadcast -- what actually matters is the target's own ARP cache
// update, not whether the rest of the segment also observes the
// frame).
func (cl *Client) Reply(senderIP net.IP, senderMAC net.HardwareAddr, dstIP net.IP, dstMAC net.HardwareAddr) error {
	pkt, err := arp.NewPacket(arp.OperationReply, senderMAC, senderIP, dstMAC, dstIP)
	if err != nil {
		return err
	}
	return cl.c.WriteTo(pkt, dstMAC)
}
