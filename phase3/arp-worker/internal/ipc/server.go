package ipc

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"sync"
)

// Server is a single-connection-at-a-time Unix domain socket IPC
// server. Only one controller process should ever be driving a given
// worker at once, so a second concurrent connection is handled
// sequentially (Serve accepts one at a time) rather than as a
// supported multiplexed mode.
type Server struct {
	ln         *net.UnixListener
	handler    Handler
	allowedUID uint32

	// encMu guards both enc itself (set/cleared as connections come and
	// go) and every write through it -- Notify (below) can be called at
	// any time from any goroutine (e.g. a LeaseMonitor's own timer,
	// entirely independent of the request/response loop in handleConn),
	// so a plain field read/write here would race handleConn's own
	// writes and could interleave two Encode() calls' bytes on the wire.
	encMu sync.Mutex
	enc   *json.Encoder // nil whenever no connection is currently active
}

// Listen creates (removing any stale socket file first) and starts
// listening on path. allowedUID restricts accepted connections to
// that peer UID via SO_PEERCRED, per
// docs/design/phase3-technical-design.md section 4 ("verified by UID,
// not just socket path"). The actual credential check lives in
// peercred_unix.go (Linux-only, matching this project's only target
// platform).
func Listen(path string, allowedUID uint32, handler Handler) (*Server, error) {
	if err := unixRemoveStale(path); err != nil {
		return nil, fmt.Errorf("remove stale socket %s: %w", path, err)
	}
	addr, err := net.ResolveUnixAddr("unix", path)
	if err != nil {
		return nil, fmt.Errorf("resolve socket addr: %w", err)
	}
	ln, err := net.ListenUnix("unix", addr)
	if err != nil {
		return nil, fmt.Errorf("listen on %s: %w", path, err)
	}
	return &Server{ln: ln, handler: handler, allowedUID: allowedUID}, nil
}

// Close stops accepting new connections. It does not affect a
// connection already being handled by Serve's goroutine.
func (s *Server) Close() error {
	return s.ln.Close()
}

// Serve accepts connections one at a time until the listener is
// closed. On a clean shutdown (Close called from another goroutine)
// AcceptUnix returns an error wrapping "use of closed network
// connection" -- callers should treat that specific case as expected,
// not a failure worth alarming on.
func (s *Server) Serve() error {
	for {
		conn, err := s.ln.AcceptUnix()
		if err != nil {
			return err
		}
		s.handleConn(conn)
	}
}

// setEncoder installs (or clears, when enc is nil) the encoder Notify
// writes to, guarded by encMu the same as every other access.
func (s *Server) setEncoder(enc *json.Encoder) {
	s.encMu.Lock()
	s.enc = enc
	s.encMu.Unlock()
}

// encode writes msg through the active connection's encoder, holding
// encMu for the duration -- the one place handleConn's own
// request/response writes actually touch the wire, so they can never
// interleave with a concurrent Notify() call from another goroutine.
func (s *Server) encode(msg any) error {
	s.encMu.Lock()
	defer s.encMu.Unlock()
	if s.enc == nil {
		return fmt.Errorf("no active connection")
	}
	return s.enc.Encode(msg)
}

// Notify sends an unsolicited message (e.g. a Fault) to the currently
// connected controller, if any -- the mechanism a background goroutine
// with no pending request of its own (a LeaseMonitor's expiry timer,
// see cmd/pp-arp-worker/main.go's onLeaseExpired) uses to actually tell
// the controller something happened, rather than only logging locally.
//
// There is no queue: if no connection is active right now, this
// returns an error and the message is simply never sent. A controller
// that reconnects afterward won't retroactively see it -- matching this
// protocol's existing posture elsewhere (a briefly-desynced heartbeat
// self-corrects on the controller's own next attempt) rather than
// adding message persistence for a case that, in practice, means "the
// controller already isn't there to read this anyway."
//
// Because a message sent here rides the SAME stream handleConn's
// request/response loop reads from, the controller receives it as the
// "reply" to whatever request it sends next, not as a distinctly-typed
// push -- controller/ipc_client.py's _request() already treats any
// op="fault" reply as a WorkerError regardless of which request
// preceded it, so this works with the existing wire protocol, not
// against it. See docs/design/phase3-technical-design.md section on
// the fault message shape.
func (s *Server) Notify(msg any) error {
	return s.encode(msg)
}

func (s *Server) handleConn(conn *net.UnixConn) {
	defer conn.Close()

	ok, _, err := peerAllowed(conn, s.allowedUID)
	if err != nil || !ok {
		// Reject by simply closing -- no error frame sent to an
		// unauthorized peer, so as not to confirm the socket's
		// protocol to anything that isn't already the controller.
		return
	}

	scanner := bufio.NewScanner(conn)
	scanner.Buffer(make([]byte, 0, 4096), 1<<20) // generous but bounded line size
	s.setEncoder(json.NewEncoder(conn))
	defer s.setEncoder(nil)

	for scanner.Scan() {
		// Copy out of the scanner's reused buffer before handing it
		// to dispatch/json.Unmarshal, which may retain slices of it
		// (e.g. inside error values) past the next Scan() call.
		line := append([]byte(nil), scanner.Bytes()...)
		if len(line) == 0 {
			continue
		}

		var env Envelope
		if err := json.Unmarshal(line, &env); err != nil {
			_ = s.encode(Fault{V: ProtocolVersion, Op: "fault", Reason: "malformed_json", Action: "connection_closed"})
			return
		}
		if env.V != ProtocolVersion {
			_ = s.encode(Fault{V: ProtocolVersion, Op: "fault", Reason: "unsupported_version", Action: "connection_closed"})
			return
		}

		replies, terminate := dispatch(s.handler, env.Op, line)
		for _, r := range replies {
			if err := s.encode(r); err != nil {
				return
			}
		}
		if terminate {
			return
		}
	}
}
