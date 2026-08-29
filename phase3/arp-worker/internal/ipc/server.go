package ipc

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
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
	enc := json.NewEncoder(conn)

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
			_ = enc.Encode(Fault{V: ProtocolVersion, Op: "fault", Reason: "malformed_json", Action: "connection_closed"})
			return
		}
		if env.V != ProtocolVersion {
			_ = enc.Encode(Fault{V: ProtocolVersion, Op: "fault", Reason: "unsupported_version", Action: "connection_closed"})
			return
		}

		replies, terminate := dispatch(s.handler, env.Op, line)
		for _, r := range replies {
			if err := enc.Encode(r); err != nil {
				return
			}
		}
		if terminate {
			return
		}
	}
}
