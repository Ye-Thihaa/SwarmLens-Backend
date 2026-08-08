/**
 * APP B — Operator Console types.
 *
 * Deliberately separate from src/guest/*: no shared components, no shared
 * data, no role flag. The only thing the two apps share is
 * src/styles.css tokens. Real state comes entirely from polling GET
 * /health on every node in src/lib/api.ts's CLUSTER -- nothing here is
 * mock data. Deploy target: laptop → room projector. Auth: operator key
 * (guests have none) -- purely a client-side gate, since main.py has no
 * real auth to check against.
 */

export type NodeState = "leader" | "follower" | "candidate" | "dead" | "partitioned";
