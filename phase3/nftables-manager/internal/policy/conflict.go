package policy

import "sort"

// Conflict records that an IP was requested in more than one policy
// set at once -- ambiguous input the caller (controller) should log
// and treat as a bug in its own desired-state computation, not
// something to silently paper over without a trace.
type Conflict struct {
	IP       string
	Sets     []SetName // every set that requested this IP, in priority order
	Resolved SetName   // which set it was actually kept in
}

// ResolveConflicts returns a DesiredPolicy where every IP appears in at
// most one set, keeping each conflicting IP only in its
// highest-priority set (AllSetNames order: bypass > authenticated >
// unauthenticated > quarantine, matching the design skeleton's
// prerouting chain evaluation order -- bypass short-circuits via
// `return` before anything else is checked, so an IP that's also
// listed in quarantine but belongs in bypass must never be dropped),
// plus the list of conflicts found so the caller can log them. An
// empty conflict list means the input was already well-formed.
//
// Output ordering is deterministic (sorted by IP) so callers -- and
// this package's own tests -- can compare results without needing an
// order-insensitive comparison.
func ResolveConflicts(desired DesiredPolicy) (DesiredPolicy, []Conflict) {
	membership := make(map[string][]SetName)
	for _, name := range AllSetNames { // already priority-ordered
		for _, ip := range desired.ByName(name) {
			membership[ip] = append(membership[ip], name)
		}
	}

	ips := make([]string, 0, len(membership))
	for ip := range membership {
		ips = append(ips, ip)
	}
	sort.Strings(ips)

	resolved := DesiredPolicy{}
	var conflicts []Conflict
	for _, ip := range ips {
		sets := membership[ip]
		winner := sets[0] // membership was built in priority order, so sets[0] is the highest priority
		if len(sets) > 1 {
			conflicts = append(conflicts, Conflict{
				IP:       ip,
				Sets:     append([]SetName(nil), sets...),
				Resolved: winner,
			})
		}
		resolved = appendIP(resolved, winner, ip)
	}
	return resolved, conflicts
}

func appendIP(d DesiredPolicy, name SetName, ip string) DesiredPolicy {
	switch name {
	case SetAuthenticated:
		d.Authenticated = append(d.Authenticated, ip)
	case SetUnauthenticated:
		d.Unauthenticated = append(d.Unauthenticated, ip)
	case SetBypass:
		d.Bypass = append(d.Bypass, ip)
	case SetQuarantine:
		d.Quarantine = append(d.Quarantine, ip)
	}
	return d
}
