//go:build linux

// Package nft adapts sigs.k8s.io/knftables to the pure
// policy.Reconcile/ResolveConflicts logic in ../policy. This is the one
// piece of nftables-manager that actually needs CAP_NET_ADMIN.
//
// NOT VERIFIED AGAINST A REAL BUILD as first written -- same situation
// as phase3/arp-worker/internal/arpio's mdlayher/arp adapter before its
// own fix: written from memory of knftables' documented shape (as used
// in kube-proxy), not checked against a fetched copy, because no Go
// toolchain was available while writing it. Expect API mismatches here
// specifically -- fix against `go doc sigs.k8s.io/knftables` once
// fetched, same workflow that fixed the ARP worker's adapter.
package nft

import (
	"context"
	"fmt"

	"sigs.k8s.io/knftables"

	"github.com/J1nx888/parental_proxy/phase3/nftables-manager/internal/policy"
)

// Manager wraps a knftables.Interface scoped to the dedicated
// "parental_proxy" table -- nothing here ever touches any other table
// on the box, so this can't interfere with other firewall rules.
type Manager struct {
	nft knftables.Interface
}

// allManagedSets is every nftables set this package creates and reads
// -- policy.AllSetNames' four mutually-exclusive classes plus the
// orthogonal policy.SetBump. Deliberately not the same slice as
// policy.AllSetNames: that list's contract is "priority-ordered,
// mutually exclusive" (see its doc comment), which bump_v4 doesn't
// satisfy, but EnsureBaseline/ReadActual just need "every set this
// process manages," so they get their own list instead of overloading
// AllSetNames' meaning.
var allManagedSets = append(append([]policy.SetName{}, policy.AllSetNames...), policy.SetBump)

// New opens a knftables interface for the `inet` family's
// "parental_proxy" table. Requires CAP_NET_ADMIN.
func New() (*Manager, error) {
	nft, err := knftables.New(knftables.InetFamily, "parental_proxy")
	if err != nil {
		return nil, fmt.Errorf("open knftables interface: %w", err)
	}
	return &Manager{nft: nft}, nil
}

// EnsureBaseline creates the table, the five named sets (allManagedSets), and the
// prerouting redirect chain if they don't already exist, and
// (re-)establishes the redirect rules -- fully idempotent, safe to
// call on every startup regardless of whether the table already has
// the baseline from a previous run (e.g. this process restarting under
// systemd's Restart=on-failure). Matches
// docs/design/phase3-technical-design.md section 5's skeleton exactly;
// keep the two in sync if either changes.
//
// The table/set/chain Add() calls are natively idempotent (knftables'
// Add is "ensure it exists," matching `nft add table` etc.) -- the one
// piece that ISN'T is rules, which Add() always appends rather than
// deduplicating by content (rules have no name/identity the way
// tables/sets/chains do). Flushing the chain before re-adding the
// rules is what makes a second EnsureBaseline call converge back to
// exactly the baseline rule set instead of appending a duplicate copy
// every restart -- verified by TestEnsureBaseline_IsIdempotentAcrossRepeatedCalls.
func (m *Manager) EnsureBaseline(ctx context.Context) error {
	tx := m.nft.NewTransaction()

	tx.Add(&knftables.Table{
		Comment: knftables.PtrTo("parental_proxy interception policy -- see RoadMap.md"),
	})

	for _, name := range allManagedSets {
		tx.Add(&knftables.Set{
			Name:  string(name),
			Type:  "ipv4_addr",
			Flags: []knftables.SetFlag{knftables.IntervalFlag},
		})
	}

	tx.Add(&knftables.Chain{
		Name:     "prerouting",
		Type:     knftables.PtrTo(knftables.NATType),
		Hook:     knftables.PtrTo(knftables.PreroutingHook),
		Priority: knftables.PtrTo(knftables.DNATPriority),
	})

	// Clear any rules a previous EnsureBaseline call left in this chain
	// before re-adding the baseline below -- a no-op the very first time
	// (empty chain), and what makes a restart not duplicate rules.
	tx.Flush(&knftables.Chain{Name: "prerouting"})

	for _, rule := range baselineRules {
		tx.Add(&knftables.Rule{Chain: "prerouting", Rule: rule})
	}

	return m.nft.Run(ctx, tx)
}

// baselineRules are the redirect rules from the design skeleton,
// applied in the order given (evaluation order matters -- bypass must
// be checked, and short-circuit via `return`, before anything else).
//
// Corrected 2026-08-30 for the "two independent axes" architecture
// (RoadMap.md, locked that date): the old ruleset redirected EVERY
// authenticated_v4 device's tcp 80/443 to Squid unconditionally, which
// was wrong -- Squid access is a separate, admin-chosen per-device
// opt-in (bump_v4), not a consequence of authentication. authenticated_v4
// now only carries its DNS redirect; bump_v4 independently carries the
// tcp 80/443 redirect to Squid's intercept ports, and composes with
// (does not replace) authenticated_v4 -- a device is normally a member
// of both. Squid itself narrows this all-or-nothing per-device redirect
// down to specific domains via its own unchanged SNI splice/bump logic
// (nftables can't see hostnames below the TLS layer, so it can't be
// selective by domain the way Squid can) -- see RoadMap.md's Squid
// intercept-mode section.
var baselineRules = []string{
	"ip saddr @bypass_v4 return",
	"ip saddr @bump_v4 tcp dport 80 redirect to :3129",
	"ip saddr @bump_v4 tcp dport 443 redirect to :3130",
	"ip saddr @authenticated_v4 udp dport 53 redirect to :5353",
	"ip saddr @authenticated_v4 tcp dport 53 redirect to :5353",
	"ip saddr @unauthenticated_v4 udp dport 53 redirect to :5353",
	"ip saddr @unauthenticated_v4 tcp dport 80 redirect to :3131",
	"ip saddr @quarantine_v4 counter drop",
}

// ReadActual reads the live membership of all five sets (allManagedSets)
// from the kernel -- never cached, matching policy.ActualPolicy's own
// doc comment on why a reconciler must always read fresh.
func (m *Manager) ReadActual(ctx context.Context) (policy.ActualPolicy, error) {
	actual := make(policy.ActualPolicy, len(allManagedSets))
	for _, name := range allManagedSets {
		elements, err := m.nft.ListElements(ctx, "set", string(name))
		if err != nil {
			return nil, fmt.Errorf("list elements of %s: %w", name, err)
		}
		ips := make([]string, 0, len(elements))
		for _, el := range elements {
			if len(el.Key) > 0 {
				ips = append(ips, el.Key[0])
			}
		}
		actual[name] = ips
	}
	return actual, nil
}

// ApplyDiffs applies every set's add/remove changes in ONE atomic
// transaction -- either all of it lands, or (per knftables' own
// atomic-transaction guarantee cited in the design doc section 1)
// none of it does. This is what makes Milestone 5's "atomic
// apply/rollback" requirement concrete.
func (m *Manager) ApplyDiffs(ctx context.Context, diffs map[policy.SetName]policy.SetDiff) error {
	if len(diffs) == 0 {
		return nil
	}
	tx := m.nft.NewTransaction()
	for name, diff := range diffs {
		for _, ip := range diff.Add {
			tx.Add(&knftables.Element{Set: string(name), Key: []string{ip}})
		}
		for _, ip := range diff.Remove {
			tx.Delete(&knftables.Element{Set: string(name), Key: []string{ip}})
		}
	}
	return m.nft.Run(ctx, tx)
}
