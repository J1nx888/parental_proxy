package ipc

import (
	"encoding/json"
	"testing"
)

// fakeHandler is a test double for Handler -- lets dispatch's routing
// and error behavior be verified with no real socket at all.
type fakeHandler struct {
	lastReplace   *ReplaceTargets
	lastHeartbeat *Heartbeat
	lastShutdown  *ShutdownMsg
}

func (f *fakeHandler) HandleReplaceTargets(m ReplaceTargets) []any {
	f.lastReplace = &m
	return []any{GenerationApplied{V: ProtocolVersion, Op: "generation_applied", Generation: m.Generation, TargetCount: len(m.Targets)}}
}

func (f *fakeHandler) HandleHeartbeat(m Heartbeat) []any {
	f.lastHeartbeat = &m
	return []any{HeartbeatAck{V: ProtocolVersion, Op: "heartbeat_ack", Sequence: m.Sequence}}
}

func (f *fakeHandler) HandleShutdown(m ShutdownMsg) []any {
	f.lastShutdown = &m
	return nil
}

func TestDispatch_ReplaceTargets(t *testing.T) {
	raw, _ := json.Marshal(ReplaceTargets{
		V: ProtocolVersion, Op: "replace_targets", Generation: 43,
		Gateway: Target{IP: "192.168.1.1", MAC: "aa:bb:cc:dd:ee:01"},
		Targets: []Target{{IP: "192.168.1.21", MAC: "aa:bb:cc:dd:ee:22"}},
	})
	h := &fakeHandler{}
	replies, terminate := dispatch(h, "replace_targets", raw)
	if terminate {
		t.Fatal("replace_targets should not terminate the connection")
	}
	if h.lastReplace == nil || h.lastReplace.Generation != 43 {
		t.Fatalf("handler did not receive the parsed message, got %+v", h.lastReplace)
	}
	if len(replies) != 1 {
		t.Fatalf("expected one generation_applied reply, got %d", len(replies))
	}
}

func TestDispatch_HeartbeatRoundTrips(t *testing.T) {
	raw, _ := json.Marshal(Heartbeat{V: ProtocolVersion, Op: "heartbeat", Sequence: 8842})
	h := &fakeHandler{}
	replies, terminate := dispatch(h, "heartbeat", raw)
	if terminate {
		t.Fatal("heartbeat should not terminate the connection")
	}
	if h.lastHeartbeat == nil || h.lastHeartbeat.Sequence != 8842 {
		t.Fatalf("handler did not receive the parsed sequence, got %+v", h.lastHeartbeat)
	}
	ack, ok := replies[0].(HeartbeatAck)
	if !ok || ack.Sequence != 8842 {
		t.Fatalf("expected a HeartbeatAck echoing sequence 8842, got %+v", replies)
	}
}

func TestDispatch_ShutdownTerminates(t *testing.T) {
	raw, _ := json.Marshal(ShutdownMsg{V: ProtocolVersion, Op: "shutdown", Reason: "controller_requested"})
	h := &fakeHandler{}
	_, terminate := dispatch(h, "shutdown", raw)
	if !terminate {
		t.Fatal("shutdown must terminate the connection")
	}
	if h.lastShutdown == nil || h.lastShutdown.Reason != "controller_requested" {
		t.Fatalf("handler did not receive the parsed reason, got %+v", h.lastShutdown)
	}
}

func TestDispatch_UnknownOpTerminates(t *testing.T) {
	h := &fakeHandler{}
	replies, terminate := dispatch(h, "not_a_real_op", []byte(`{"v":1,"op":"not_a_real_op"}`))
	if !terminate {
		t.Fatal("an unknown op should terminate the connection, not be silently ignored")
	}
	if len(replies) != 1 {
		t.Fatalf("expected one fault reply, got %d", len(replies))
	}
}

func TestDispatch_MalformedShutdownTerminatesWithoutCallingHandler(t *testing.T) {
	h := &fakeHandler{}
	// Deliberately truncated JSON body for the op.
	replies, terminate := dispatch(h, "shutdown", []byte(`{"v":1,"op":"shutdown","reason":`))
	if !terminate {
		t.Fatal("a malformed shutdown body should terminate the connection")
	}
	if h.lastShutdown != nil {
		t.Fatal("handler should not have been called with a malformed message")
	}
	if len(replies) != 1 {
		t.Fatalf("expected one fault reply, got %d", len(replies))
	}
}
