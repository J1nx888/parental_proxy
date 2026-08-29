package policy

import "testing"

func TestReconcile_NoChangesWhenAlreadyMatching(t *testing.T) {
	desired := DesiredPolicy{
		Authenticated: []string{"192.168.1.21"},
		Bypass:        []string{"192.168.1.1"},
	}
	actual := ActualPolicy{
		SetAuthenticated: []string{"192.168.1.21"},
		SetBypass:        []string{"192.168.1.1"},
	}
	diffs := Reconcile(desired, actual)
	if len(diffs) != 0 {
		t.Fatalf("expected no diffs when everything already matches, got %+v", diffs)
	}
}

func TestReconcile_AddsNewMembers(t *testing.T) {
	desired := DesiredPolicy{Authenticated: []string{"192.168.1.21", "192.168.1.22"}}
	actual := ActualPolicy{SetAuthenticated: []string{"192.168.1.21"}}
	diffs := Reconcile(desired, actual)

	d, ok := diffs[SetAuthenticated]
	if !ok {
		t.Fatal("expected a diff for authenticated_v4")
	}
	if len(d.Add) != 1 || d.Add[0] != "192.168.1.22" {
		t.Fatalf("expected to add 192.168.1.22, got %+v", d)
	}
	if len(d.Remove) != 0 {
		t.Fatalf("expected no removals, got %+v", d)
	}
}

func TestReconcile_RemovesStaleMembers(t *testing.T) {
	desired := DesiredPolicy{Authenticated: []string{"192.168.1.21"}}
	actual := ActualPolicy{SetAuthenticated: []string{"192.168.1.21", "192.168.1.99"}}
	diffs := Reconcile(desired, actual)

	d := diffs[SetAuthenticated]
	if len(d.Remove) != 1 || d.Remove[0] != "192.168.1.99" {
		t.Fatalf("expected to remove 192.168.1.99, got %+v", d)
	}
}

func TestReconcile_MultipleSetsDiffIndependently(t *testing.T) {
	desired := DesiredPolicy{
		Authenticated: []string{"192.168.1.21"},
		Quarantine:    []string{"192.168.1.50"},
	}
	actual := ActualPolicy{
		SetAuthenticated: []string{},                 // needs 192.168.1.21 added
		SetBypass:        []string{"192.168.1.1"},    // desired has none here -> needs removal
	}
	diffs := Reconcile(desired, actual)

	if got := diffs[SetAuthenticated]; len(got.Add) != 1 || got.Add[0] != "192.168.1.21" {
		t.Fatalf("authenticated diff wrong: %+v", got)
	}
	if got := diffs[SetBypass]; len(got.Remove) != 1 || got.Remove[0] != "192.168.1.1" {
		t.Fatalf("bypass diff wrong: %+v", got)
	}
	if got := diffs[SetQuarantine]; len(got.Add) != 1 || got.Add[0] != "192.168.1.50" {
		t.Fatalf("quarantine diff wrong: %+v", got)
	}
	if _, ok := diffs[SetUnauthenticated]; ok {
		t.Fatalf("unauthenticated should have no diff, got %+v", diffs[SetUnauthenticated])
	}
}

func TestReconcile_EmptyEverythingIsANoOp(t *testing.T) {
	diffs := Reconcile(DesiredPolicy{}, ActualPolicy{})
	if len(diffs) != 0 {
		t.Fatalf("expected no diffs for empty desired/actual, got %+v", diffs)
	}
}
