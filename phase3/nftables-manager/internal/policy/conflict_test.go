package policy

import "testing"

func namesEqual(t *testing.T, got, want []SetName) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
}

func TestResolveConflicts_NoConflictsPassesThroughUnchanged(t *testing.T) {
	desired := DesiredPolicy{
		Authenticated: []string{"192.168.1.21"},
		Bypass:        []string{"192.168.1.1"},
	}
	resolved, conflicts := ResolveConflicts(desired)
	if len(conflicts) != 0 {
		t.Fatalf("expected no conflicts, got %+v", conflicts)
	}
	if len(resolved.Authenticated) != 1 || resolved.Authenticated[0] != "192.168.1.21" {
		t.Fatalf("authenticated set changed unexpectedly: %+v", resolved)
	}
	if len(resolved.Bypass) != 1 || resolved.Bypass[0] != "192.168.1.1" {
		t.Fatalf("bypass set changed unexpectedly: %+v", resolved)
	}
}

func TestResolveConflicts_BypassWinsOverQuarantine(t *testing.T) {
	desired := DesiredPolicy{
		Bypass:     []string{"192.168.1.1"},
		Quarantine: []string{"192.168.1.1"},
	}
	resolved, conflicts := ResolveConflicts(desired)

	if len(conflicts) != 1 || conflicts[0].Resolved != SetBypass {
		t.Fatalf("expected one conflict resolved to bypass, got %+v", conflicts)
	}
	if len(resolved.Bypass) != 1 || len(resolved.Quarantine) != 0 {
		t.Fatalf("bypass must win: bypass=%v quarantine=%v", resolved.Bypass, resolved.Quarantine)
	}
}

func TestResolveConflicts_AuthenticatedWinsOverUnauthenticatedAndQuarantine(t *testing.T) {
	desired := DesiredPolicy{
		Authenticated:   []string{"192.168.1.21"},
		Unauthenticated: []string{"192.168.1.21"},
		Quarantine:      []string{"192.168.1.21"},
	}
	resolved, conflicts := ResolveConflicts(desired)

	if len(conflicts) != 1 || conflicts[0].Resolved != SetAuthenticated {
		t.Fatalf("expected authenticated to win, got %+v", conflicts)
	}
	if len(resolved.Authenticated) != 1 || len(resolved.Unauthenticated) != 0 || len(resolved.Quarantine) != 0 {
		t.Fatalf("only authenticated should retain the IP: %+v", resolved)
	}
}

func TestResolveConflicts_ConflictRecordsEverySetInPriorityOrder(t *testing.T) {
	desired := DesiredPolicy{
		Bypass:     []string{"192.168.1.1"},
		Quarantine: []string{"192.168.1.1"},
	}
	_, conflicts := ResolveConflicts(desired)
	if len(conflicts) != 1 {
		t.Fatalf("expected exactly one conflict, got %+v", conflicts)
	}
	namesEqual(t, conflicts[0].Sets, []SetName{SetBypass, SetQuarantine})
}

func TestResolveConflicts_BumpPassesThroughWhenAlsoAuthenticated(t *testing.T) {
	desired := DesiredPolicy{
		Authenticated: []string{"192.168.1.21"},
		Bump:          []string{"192.168.1.21"},
	}
	resolved, conflicts := ResolveConflicts(desired)
	if len(conflicts) != 0 {
		t.Fatalf("expected no conflicts, got %+v", conflicts)
	}
	if len(resolved.Authenticated) != 1 || resolved.Authenticated[0] != "192.168.1.21" {
		t.Fatalf("authenticated set changed unexpectedly: %+v", resolved)
	}
	if len(resolved.Bump) != 1 || resolved.Bump[0] != "192.168.1.21" {
		t.Fatalf("expected bump to also carry the IP, got %+v", resolved.Bump)
	}
}

func TestResolveConflicts_BumpDroppedWithoutAuthenticated(t *testing.T) {
	// A device flagged bump_enabled while also ignored/quarantined (a
	// bug in the caller's own desired-state computation) must not put
	// its IP into bump_v4 unguarded -- the hard-deny invariant depends
	// on bump only ever composing with an actually-authenticated device.
	desired := DesiredPolicy{
		Bypass: []string{"192.168.1.5"},
		Bump:   []string{"192.168.1.5"},
	}
	resolved, conflicts := ResolveConflicts(desired)
	if len(resolved.Bump) != 0 {
		t.Fatalf("expected bump to be dropped when the IP isn't authenticated, got %+v", resolved.Bump)
	}
	found := false
	for _, c := range conflicts {
		if c.IP == "192.168.1.5" && len(c.Sets) == 1 && c.Sets[0] == SetBump {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected a recorded conflict for the dropped bump IP, got %+v", conflicts)
	}
}

func TestResolveConflicts_BumpDeduplicatedAndSorted(t *testing.T) {
	desired := DesiredPolicy{
		Authenticated: []string{"192.168.1.10", "192.168.1.20"},
		Bump:          []string{"192.168.1.20", "192.168.1.10", "192.168.1.20"},
	}
	resolved, _ := ResolveConflicts(desired)
	want := []string{"192.168.1.10", "192.168.1.20"}
	if len(resolved.Bump) != len(want) {
		t.Fatalf("resolved.Bump = %v, want %v", resolved.Bump, want)
	}
	for i, ip := range want {
		if resolved.Bump[i] != ip {
			t.Fatalf("resolved.Bump = %v, want sorted+deduped %v", resolved.Bump, want)
		}
	}
}

func TestResolveConflicts_OutputIsSortedByIP(t *testing.T) {
	desired := DesiredPolicy{Authenticated: []string{"192.168.1.30", "192.168.1.10", "192.168.1.20"}}
	resolved, _ := ResolveConflicts(desired)
	want := []string{"192.168.1.10", "192.168.1.20", "192.168.1.30"}
	if len(resolved.Authenticated) != len(want) {
		t.Fatalf("resolved.Authenticated = %v, want %v", resolved.Authenticated, want)
	}
	for i, ip := range want {
		if resolved.Authenticated[i] != ip {
			t.Fatalf("resolved.Authenticated = %v, want sorted %v", resolved.Authenticated, want)
		}
	}
}
