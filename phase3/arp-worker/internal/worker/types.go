// Package worker implements the ARP poisoning/restoration scheduler
// described in docs/design/phase3-technical-design.md section 3. It
// is deliberately independent of any specific raw-socket library --
// see the ARPSender interface below -- so the scheduling, lease, and
// safety-check logic in this package can be unit tested without
// CAP_NET_RAW, a real NIC, or root. The production adapter wiring
// github.com/mdlayher/arp lives in the sibling ../arpio package.
package worker

import (
	"net"
	"time"
)

// Target is one device the worker is actively poisoning: its real IP
// and its real (unspoofed) hardware address, needed so corrective ARPs
// can restore the truth later.
type Target struct {
	IP  net.IP
	MAC net.HardwareAddr
}

// Generation is one immutable snapshot of "who to poison right now,"
// matching RoadMap.md's "one scheduler operating on an immutable
// per-generation target snapshot, not a thread per host" requirement.
// A new Generation from the controller always fully replaces the
// previous one -- there is no incremental add/remove of individual
// targets within a running generation.
type Generation struct {
	ID         uint64
	Gateway    Target
	Targets    []Target
	FullDuplex bool
}

// ARPSender abstracts sending/receiving ARP packets so this package's
// scheduling and safety-check logic can be unit tested without a real
// NIC or CAP_NET_RAW. See ../arpio for the production implementation
// (wraps github.com/mdlayher/arp).
type ARPSender interface {
	// Reply sends one ARP reply claiming senderIP is at senderMAC,
	// addressed on the wire to dstMAC. Used both for poisoning
	// (senderMAC = the worker's own MAC) and for corrective
	// restoration (senderMAC = the real owner's MAC).
	Reply(senderIP net.IP, senderMAC net.HardwareAddr, dstIP net.IP, dstMAC net.HardwareAddr) error

	// Resolve performs a genuine ARP request/reply exchange to learn
	// ip's real hardware address. Must never be satisfied from a
	// cache -- see ResolveGateway in safety.go for why.
	Resolve(ip net.IP) (net.HardwareAddr, error)

	Close() error
}

// Config holds the tunable constants that RoadMap.md section 8
// explicitly flags as "not decided here, needs real numbers from the
// soak-test milestone." The values in DefaultConfig are placeholders
// only -- do not treat them as tuned.
type Config struct {
	Interval          time.Duration
	CorrectiveRepeats int
	CorrectiveSpacing time.Duration

	// OnSendError, if non-nil, is called for every ARPSender.Reply()
	// failure -- added 2026-08-30 after a real, silent failure went
	// completely unnoticed during this project's first live-container
	// verification pass (see worker.go's sendGratuitousReply, which
	// used to just discard the error with `_ = err`): the poisoning
	// loop applied cleanly, the controller reported success, and
	// nothing anywhere logged that not a single ARP packet was actually
	// going out. Optional (nil is fine, matches every existing caller
	// unchanged) rather than a required constructor parameter, since
	// this is observability, not a behavior change -- the loop must
	// still keep running through individual send failures (see this
	// TODO's own original reasoning, unchanged: a single dropped frame
	// must not tear down the whole generation). Does not yet escalate
	// to a controller "fault" IPC message on sustained failure -- see
	// sendGratuitousReply's own remaining TODO for that.
	OnSendError func(error)
}

// DefaultConfig returns conservative placeholder values. See the
// Config doc comment -- these are explicitly not the soak-tested
// numbers RoadMap.md calls for.
func DefaultConfig() Config {
	return Config{
		Interval:          2 * time.Second,
		CorrectiveRepeats: 5,
		CorrectiveSpacing: 200 * time.Millisecond,
	}
}
