package policy

import "sort"

// SetDiff is what needs to change in one nftables set to match desired
// state: elements to add and elements to remove. Both sorted for
// deterministic output.
type SetDiff struct {
	Add    []string
	Remove []string
}

// Empty reports whether this diff requires no changes at all.
func (d SetDiff) Empty() bool {
	return len(d.Add) == 0 && len(d.Remove) == 0
}

// Reconcile computes the per-set changes needed to bring actual in
// line with desired. Only sets that actually differ appear in the
// returned map -- an unchanged set is omitted entirely (not included
// with an Empty() diff), so callers can build an atomic transaction
// from exactly (and only) what changed. Mirrors the ARP worker's own
// "idempotent reconciliation" requirement (RoadMap.md Milestone 3) on
// the firewall side: re-running this against unchanged state must be
// a true no-op, not a needless empty transaction.
//
// Callers should pass desired through ResolveConflicts first --
// Reconcile itself does not check for an IP appearing in more than one
// set.
func Reconcile(desired DesiredPolicy, actual ActualPolicy) map[SetName]SetDiff {
	diffs := make(map[SetName]SetDiff)
	for _, name := range AllSetNames {
		d := diffSet(desired.ByName(name), actual[name])
		if !d.Empty() {
			diffs[name] = d
		}
	}
	return diffs
}

func diffSet(desired, actual []string) SetDiff {
	desiredSet := toSet(desired)
	actualSet := toSet(actual)

	var add, remove []string
	for ip := range desiredSet {
		if !actualSet[ip] {
			add = append(add, ip)
		}
	}
	for ip := range actualSet {
		if !desiredSet[ip] {
			remove = append(remove, ip)
		}
	}
	sort.Strings(add)
	sort.Strings(remove)
	return SetDiff{Add: add, Remove: remove}
}

func toSet(ips []string) map[string]bool {
	s := make(map[string]bool, len(ips))
	for _, ip := range ips {
		s[ip] = true
	}
	return s
}
