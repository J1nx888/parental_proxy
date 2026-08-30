package dbsource

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"
)

// setupDB creates a throwaway SQLite file with just enough schema
// (interception_runtime's singleton row) for ReadDesiredPolicy/WriteHealth
// to exercise against -- a minimal stand-in for common/db.py's real
// migration, since this package only ever touches this one table.
func setupDB(t *testing.T, desiredPolicyJSON string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.db")

	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("open %s: %v", path, err)
	}
	defer db.Close()

	if _, err := db.Exec(`
		CREATE TABLE interception_runtime (
			singleton_id INTEGER PRIMARY KEY,
			desired_policy_json TEXT,
			nft_mode TEXT,
			nft_last_healthy_at TEXT,
			nft_fail_reason TEXT
		)`); err != nil {
		t.Fatalf("create table: %v", err)
	}

	if desiredPolicyJSON != "" {
		if _, err := db.Exec(
			"INSERT INTO interception_runtime (singleton_id, desired_policy_json) VALUES (1, ?)",
			desiredPolicyJSON,
		); err != nil {
			t.Fatalf("seed row: %v", err)
		}
	}

	return path
}

// Regression test for the bug found 2026-08-30 while preparing a
// live-Squid verification pass: desiredPolicyWire never declared a
// "bump" JSON field, so a real desired_policy_json blob written by
// controller/policy_state.py -- which always includes a "bump" key,
// see tests/test_controller_run_cycle.py's own expected dict -- had its
// bump membership silently discarded on every read, meaning
// pp-nftables-manager could never actually redirect any device to
// Squid's intercept ports no matter how many devices had
// bump_enabled set in the database.
func TestReadDesiredPolicy_PopulatesBumpField(t *testing.T) {
	path := setupDB(t, `{
		"authenticated": ["192.168.1.10"],
		"unauthenticated": [],
		"bypass": [],
		"quarantine": [],
		"bump": ["192.168.1.10"]
	}`)

	got, err := ReadDesiredPolicy(path)
	if err != nil {
		t.Fatalf("ReadDesiredPolicy: %v", err)
	}

	if len(got.Bump) != 1 || got.Bump[0] != "192.168.1.10" {
		t.Fatalf("Bump = %v, want [192.168.1.10]", got.Bump)
	}
	if len(got.Authenticated) != 1 || got.Authenticated[0] != "192.168.1.10" {
		t.Fatalf("Authenticated = %v, want [192.168.1.10]", got.Authenticated)
	}
}

func TestReadDesiredPolicy_NoRowReturnsZeroValue(t *testing.T) {
	path := setupDB(t, "")

	got, err := ReadDesiredPolicy(path)
	if err != nil {
		t.Fatalf("ReadDesiredPolicy: %v", err)
	}
	if len(got.Bump) != 0 || len(got.Authenticated) != 0 {
		t.Fatalf("expected zero-value DesiredPolicy, got %+v", got)
	}
}
