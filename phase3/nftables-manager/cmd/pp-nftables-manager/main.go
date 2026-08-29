// Command pp-nftables-manager is the Milestone 5 nftables-manager
// process: a small, CAP_NET_ADMIN-scoped daemon that maintains the
// dedicated "parental_proxy" nftables table and reconciles its four
// named policy sets against desired state.
//
// NOT wired into a real desired-state source yet -- this entrypoint
// currently only bootstraps the baseline table/sets/chain and exits.
// A real deployment needs a desired-state input (e.g. IPC from the
// interception-controller, or reading its own DB query analogous to
// controller/desired_state.py) and a reconciliation loop calling
// ReadActual -> policy.Reconcile -> ApplyDiffs on an interval or on
// event -- not yet built. See RoadMap.md's Milestone 5 entry.
package main

import (
	"context"
	"flag"
	"log"

	"github.com/J1nx888/parental_proxy/phase3/nftables-manager/internal/nft"
)

func main() {
	bootstrapOnly := flag.Bool(
		"bootstrap-only", true,
		"Create the baseline table/sets/chain and exit, without a reconciliation loop "+
			"(the only supported mode today -- see this file's header note).",
	)
	flag.Parse()

	mgr, err := nft.New()
	if err != nil {
		log.Fatalf("open nftables interface (needs CAP_NET_ADMIN): %v", err)
	}

	ctx := context.Background()
	if err := mgr.EnsureBaseline(ctx); err != nil {
		log.Fatalf("create baseline table/sets/chain: %v", err)
	}
	log.Print("baseline table/sets/chain created (or already present)")

	if *bootstrapOnly {
		return
	}
	log.Fatal("reconciliation loop not implemented yet -- see this file's header note")
}
