package ipc

import "encoding/json"

// Handler receives parsed controller->worker messages. Implemented by
// the glue code in cmd/pp-arp-worker/main.go that actually drives a
// worker.Worker and a worker.LeaseMonitor -- this package has no
// dependency on the worker package at all, by design.
type Handler interface {
	HandleReplaceTargets(ReplaceTargets) []any
	HandleHeartbeat(Heartbeat) []any
	HandleShutdown(ShutdownMsg) []any
}

// dispatch parses one newline-delimited JSON frame (already known to
// carry the current protocol version, checked by the caller) and
// routes it to handler. It has no dependency on net.Conn or any OS
// socket API, so it's unit tested directly (see dispatch_test.go)
// without needing a real Unix socket -- deliberately kept separate
// from server.go for exactly that reason.
func dispatch(handler Handler, op string, raw []byte) (replies []any, terminate bool) {
	switch op {
	case "replace_targets":
		var m ReplaceTargets
		if err := json.Unmarshal(raw, &m); err != nil {
			return []any{Fault{V: ProtocolVersion, Op: "fault", Reason: "malformed_replace_targets", Action: "connection_closed"}}, true
		}
		return handler.HandleReplaceTargets(m), false
	case "heartbeat":
		var m Heartbeat
		if err := json.Unmarshal(raw, &m); err != nil {
			return []any{Fault{V: ProtocolVersion, Op: "fault", Reason: "malformed_heartbeat", Action: "connection_closed"}}, true
		}
		return handler.HandleHeartbeat(m), false
	case "shutdown":
		var m ShutdownMsg
		if err := json.Unmarshal(raw, &m); err != nil {
			return []any{Fault{V: ProtocolVersion, Op: "fault", Reason: "malformed_shutdown", Action: "connection_closed"}}, true
		}
		return handler.HandleShutdown(m), true
	default:
		return []any{Fault{V: ProtocolVersion, Op: "fault", Reason: "unknown_op", Action: "connection_closed"}}, true
	}
}
