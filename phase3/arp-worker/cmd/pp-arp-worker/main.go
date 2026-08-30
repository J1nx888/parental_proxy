// Command pp-arp-worker is the privileged ARP worker process described
// in RoadMap.md and docs/design/phase3-technical-design.md. It holds
// CAP_NET_RAW and nothing else; all policy/DB/reconciliation logic
// lives in the separate interception-controller process (not yet
// written), which drives this one exclusively over the Unix socket
// IPC protocol in internal/ipc/protocol.go.
//
// NOT YET BUILT OR TESTED -- see the design doc's header note. Verify
// internal/arpio's mdlayher/arp API usage against a real `go get`
// before the first build.
package main

import (
	"context"
	"flag"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/coreos/go-systemd/v22/daemon"

	"github.com/J1nx888/parental_proxy/phase3/arp-worker/internal/arpio"
	"github.com/J1nx888/parental_proxy/phase3/arp-worker/internal/ipc"
	"github.com/J1nx888/parental_proxy/phase3/arp-worker/internal/worker"
)

func main() {
	ifaceName := flag.String("iface", "", "LAN interface to bind to (required)")
	socketPath := flag.String("socket", "/run/parental_proxy/arp-worker.sock", "controller IPC socket path")
	// -1 default (not 0) deliberately: 0 is root's real, legitimate UID, so
	// using it as the "not provided" sentinel would make this flag
	// silently un-settable to 0 -- found via a real integration test where
	// the controller happened to run as root in a container. flag.Uint
	// can't represent -1, hence Int here with an explicit range check
	// instead of Uint's implicit zero-value trap.
	controllerUID := flag.Int("controller-uid", -1, "UID the interception-controller process runs as (required, checked via SO_PEERCRED)")
	leaseMissedCycles := flag.Int("lease-missed-cycles", 5, "missed heartbeat cycles before entering repair-only mode")
	flag.Parse()

	if *ifaceName == "" || *controllerUID < 0 {
		log.Fatal("-iface and -controller-uid are both required")
	}

	ifi, err := net.InterfaceByName(*ifaceName)
	if err != nil {
		log.Fatalf("resolve interface %s: %v", *ifaceName, err)
	}

	sender, err := arpio.Dial(ifi)
	if err != nil {
		log.Fatalf("dial ARP client on %s (needs CAP_NET_RAW): %v", *ifaceName, err)
	}
	defer sender.Close()

	cfg := worker.DefaultConfig() // placeholder constants -- see RoadMap.md section 8
	cfg.OnSendError = func(err error) {
		log.Printf("ARP send failed (worker keeps running, see worker.Config.OnSendError's own doc comment): %v", err)
	}
	w := worker.New(sender, ifi.HardwareAddr, cfg)

	h := &controllerHandler{worker: w}
	h.lease = worker.NewLeaseMonitor(cfg.Interval, *leaseMissedCycles, h.onLeaseExpired)
	defer h.lease.Stop()

	srv, err := ipc.Listen(*socketPath, uint32(*controllerUID), h)
	if err != nil {
		log.Fatalf("listen on %s: %v", *socketPath, err)
	}
	defer srv.Close()

	go func() {
		if err := srv.Serve(); err != nil {
			log.Printf("ipc server stopped: %v", err)
		}
	}()

	if ok, notifyErr := daemon.SdNotify(false, daemon.SdNotifyReady); notifyErr != nil {
		log.Printf("sd_notify READY failed (non-fatal, likely not running under systemd): %v", notifyErr)
	} else if !ok {
		log.Print("sd_notify not supported here (not running under systemd) -- continuing without watchdog pings")
	} else {
		go watchdogLoop()
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	<-sig

	log.Print("shutting down: sending corrective ARPs before exit")
	w.Shutdown() // fail-open: restores real ARP state for the current generation
}

func watchdogLoop() {
	interval, err := daemon.SdWatchdogEnabled(false)
	if err != nil || interval == 0 {
		return // WatchdogSec not set in the unit file -- nothing to do
	}
	ticker := time.NewTicker(interval / 3) // notify at ~3x the required rate, standard sd_notify practice
	defer ticker.Stop()
	for range ticker.C {
		_, _ = daemon.SdNotify(false, daemon.SdNotifyWatchdog)
	}
}

// controllerHandler implements ipc.Handler, translating IPC messages
// into worker.Worker calls and lease resets. This is glue code, not
// the interesting logic -- see internal/worker for that.
type controllerHandler struct {
	worker *worker.Worker
	lease  *worker.LeaseMonitor
}

func (h *controllerHandler) HandleReplaceTargets(m ipc.ReplaceTargets) []any {
	gw := worker.Target{IP: net.ParseIP(m.Gateway.IP), MAC: parseMACOrNil(m.Gateway.MAC)}

	var targets []worker.Target
	var failures []string
	for _, t := range m.Targets {
		ip := net.ParseIP(t.IP)
		mac := parseMACOrNil(t.MAC)
		if ip == nil || mac == nil {
			failures = append(failures, t.IP)
			continue
		}
		targets = append(targets, worker.Target{IP: ip, MAC: mac})
	}

	// TODO(Milestone 2 hardening): run these through
	// worker.ValidateTargets (gateway/self/broadcast/multicast/bypass
	// rejection) before ApplyGeneration -- the controller is expected
	// to have already filtered bypass_v4 out, but the worker should
	// not trust that blindly. Needs the subnet and bypass set threaded
	// through from a controller-supplied field not yet in the wire
	// schema; left as an open item rather than guessed at here.
	gen := worker.Generation{ID: m.Generation, Gateway: gw, Targets: targets, FullDuplex: m.FullDuplex}
	h.worker.ApplyGeneration(context.Background(), gen)
	h.lease.Rearm() // a fresh replace_targets is exactly the "must not auto-resume without an explicit new generation" trigger from the design doc

	return []any{ipc.GenerationApplied{
		V: ipc.ProtocolVersion, Op: "generation_applied",
		Generation: m.Generation, TargetCount: len(targets), ResolutionFailures: failures,
	}}
}

func (h *controllerHandler) HandleHeartbeat(m ipc.Heartbeat) []any {
	h.lease.Heartbeat()
	return []any{ipc.HeartbeatAck{
		V: ipc.ProtocolVersion, Op: "heartbeat_ack",
		Sequence: m.Sequence, SentCounters: h.worker.SentCounters(),
	}}
}

func (h *controllerHandler) HandleShutdown(m ipc.ShutdownMsg) []any {
	h.worker.Shutdown()
	return nil
}

func (h *controllerHandler) onLeaseExpired() {
	log.Print("lease expired: no heartbeat received in time, entering repair-only mode")
	h.worker.Shutdown() // sends one corrective round and drops the stale generation, per the design doc's lease rule
}

func parseMACOrNil(s string) net.HardwareAddr {
	mac, err := net.ParseMAC(s)
	if err != nil {
		return nil
	}
	return mac
}
