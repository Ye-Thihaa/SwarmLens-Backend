import { useEffect, useRef, useState } from "react";
import { CLUSTER, URL_TO_ID, type NodeHealth } from "@/lib/api";

/** A live node graph for the operator console: three nodes, edges between
 * every pair, and a pulse animation fired for each REAL event this
 * console can observe -- not a scripted animation loop.
 *
 * Two kinds of pulse, each backed by an actual field in GET /health:
 *
 * - Gossip pulse (green, `--converged`): fires when a peer's
 *   `gossip.peers[url].last_ok` timestamp advances between polls. That
 *   timestamp is only ever updated by gossip.py's `_mark_ok`, right after
 *   a real POST /gossip/sync + (maybe) POST /gossip/push round actually
 *   succeeded against that specific peer -- so a pulse here means a
 *   round-trip really happened, not that 1s of wall-clock time passed.
 * - Heartbeat pulse (amber, `--drifting`): fires once per poll, from
 *   whichever node currently reports `raft.role === "leader"`, toward
 *   the other two. Raft's real heartbeat is every 50ms -- far faster
 *   than this console's 1s poll can resolve -- so this is deliberately
 *   NOT "one dot per heartbeat". It's "this node is actively heartbeating
 *   right now", sampled once per poll; the label says so rather than
 *   implying a precision this can't back up.
 *
 * Both are found by diffing the CURRENT health snapshot against the
 * PREVIOUS one, so this component needs no polling of its own -- it
 * reads whatever the console's existing 1s health loop already fetched. */

type Pulse = {
  id: string;
  fromId: string;
  toId: string;
  color: string;
  kind: "gossip" | "heartbeat";
  startedAt: number;
};

const PULSE_MS = 850;
const PRUNE_INTERVAL_MS = 120;

// Fixed triangular layout, viewBox-relative -- three nodes never need a
// force layout, and a static one means the graph doesn't jitter every
// render the way a live physics sim would.
const LAYOUT: Record<string, { x: number; y: number }> = {
  node1: { x: 200, y: 40 },
  node2: { x: 40, y: 220 },
  node3: { x: 360, y: 220 },
};

function edgeKey(a: string, b: string): string {
  return [a, b].sort().join("|");
}

/** Resolves a peer URL (as reported by that node's own GET /health) back
 * to a CLUSTER id. URL_TO_ID alone isn't enough: it's built from
 * CLUSTER's configured URLs (127.0.0.1:800x locally, or whatever
 * VITE_CLUSTER_URLS names), but a node's PEERS env var can use a
 * completely different shape for the exact same cluster -- confirmed
 * live against docker-compose.yml, whose nodes address each other as
 * http://node2:8000 (Docker service hostnames), which never appears in
 * URL_TO_ID at all. Under that shape every gossip pulse was silently
 * dropped -- heartbeat pulses (which never look up a peer URL) kept
 * firing normally, which is what made this look like "gossip just isn't
 * happening" rather than a lookup bug when it was first measured live.
 * Falling back to "does this cluster's own id appear in the URL" handles
 * both shapes without hardcoding either one. */
function resolvePeerId(url: string): string | undefined {
  return URL_TO_ID[url] ?? CLUSTER.find((n) => url.includes(n.id))?.id;
}

export function NetworkGraph({ health }: { health: Record<string, NodeHealth | null> }) {
  const [pulses, setPulses] = useState<Pulse[]>([]);
  const prevRef = useRef<Record<string, NodeHealth | null>>({});
  const seq = useRef(0);

  // Detect real events by diffing this poll's health against last poll's.
  useEffect(() => {
    const prev = prevRef.current;
    const fresh: Pulse[] = [];

    for (const n of CLUSTER) {
      const h = health[n.id];
      const p = prev[n.id];
      if (!h) continue;

      if (p) {
        for (const [peerUrl, cur] of Object.entries(h.gossip.peers)) {
          const before = p.gossip.peers[peerUrl];
          if (cur.last_ok != null && cur.last_ok !== before?.last_ok) {
            const peerId = resolvePeerId(peerUrl);
            if (peerId && LAYOUT[peerId]) {
              seq.current += 1;
              fresh.push({
                id: `p${seq.current}`,
                fromId: n.id,
                toId: peerId,
                color: "var(--converged)",
                kind: "gossip",
                startedAt: Date.now(),
              });
            }
          }
        }
      }

      if (h.raft.role === "leader") {
        for (const other of CLUSTER) {
          if (other.id === n.id) continue;
          seq.current += 1;
          fresh.push({
            id: `p${seq.current}`,
            fromId: n.id,
            toId: other.id,
            color: "var(--drifting)",
            kind: "heartbeat",
            startedAt: Date.now(),
          });
        }
      }
    }

    if (fresh.length) setPulses((old) => [...old, ...fresh]);
    prevRef.current = health;
  }, [health]);

  // One prune tick instead of a setTimeout per pulse -- animateMotion
  // handles the actual movement natively; this just clears finished ones
  // out of React state so the SVG doesn't accumulate dead elements.
  useEffect(() => {
    const id = setInterval(() => {
      setPulses((old) => old.filter((p) => Date.now() - p.startedAt < PULSE_MS));
    }, PRUNE_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);

  const nodeOf = (id: string) => health[id] ?? null;
  const roleOf = (id: string): "leader" | "candidate" | "follower" | "dead" => {
    const h = nodeOf(id);
    if (!h) return "dead";
    return h.raft.role;
  };

  // De-duplicate the 3 possible pairs so each edge (line) is drawn once,
  // regardless of which direction gossip most recently ran.
  const pairs: [string, string][] = [];
  for (let i = 0; i < CLUSTER.length; i++) {
    for (let j = i + 1; j < CLUSTER.length; j++) {
      pairs.push([CLUSTER[i]!.id, CLUSTER[j]!.id]);
    }
  }
  const recentGossipEdges = new Set(
    pulses.filter((p) => p.kind === "gossip").map((p) => edgeKey(p.fromId, p.toId)),
  );

  return (
    <svg viewBox="0 0 400 260" className="h-auto w-full">
      <defs>
        <filter id="ng-glow" x="-60%" y="-60%" width="220%" height="220%">
          <feGaussianBlur stdDeviation="2.4" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* base edges -- one gray line per pair, brightened while a gossip
          round is actively pulsing across it */}
      {pairs.map(([a, b]) => {
        const pa = LAYOUT[a]!, pb = LAYOUT[b]!;
        const hot = recentGossipEdges.has(edgeKey(a, b));
        return (
          <line
            key={`${a}-${b}`}
            x1={pa.x} y1={pa.y} x2={pb.x} y2={pb.y}
            stroke={hot ? "var(--converged)" : "var(--border)"}
            strokeWidth={hot ? 1.6 : 1}
            strokeOpacity={hot ? 0.7 : 0.6}
          />
        );
      })}

      {/* pulses -- native SVG motion, no per-frame React re-render */}
      {pulses.map((p) => {
        const from = LAYOUT[p.fromId], to = LAYOUT[p.toId];
        if (!from || !to) return null;
        return (
          <circle key={p.id} r={p.kind === "gossip" ? 4 : 3} fill={p.color} filter="url(#ng-glow)">
            <animateMotion
              dur={`${PULSE_MS}ms`}
              repeatCount="1"
              fill="freeze"
              path={`M ${from.x} ${from.y} L ${to.x} ${to.y}`}
            />
            <animate attributeName="opacity" values="1;1;0" keyTimes="0;0.8;1" dur={`${PULSE_MS}ms`} fill="freeze" />
          </circle>
        );
      })}

      {/* nodes drawn last, on top of every line/pulse */}
      {CLUSTER.map((n) => {
        const pos = LAYOUT[n.id]!;
        const role = roleOf(n.id);
        const color =
          role === "leader" ? "var(--converged)" :
          role === "dead" ? "var(--safelight)" :
          role === "candidate" ? "var(--drifting)" :
          "var(--fixer-dim)";
        return (
          <g key={n.id}>
            <circle
              cx={pos.x} cy={pos.y} r={role === "leader" ? 14 : 11}
              fill="var(--card)" stroke={color} strokeWidth={role === "leader" ? 2.5 : 1.5}
              filter={role === "leader" ? "url(#ng-glow)" : undefined}
            />
            <text
              x={pos.x} y={pos.y + (role === "leader" ? 28 : 25)}
              textAnchor="middle" fontSize="10" fontFamily="ui-monospace, monospace"
              fill={color}
              style={{ letterSpacing: "0.05em" }}
            >
              {n.id.toUpperCase()}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
