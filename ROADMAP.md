# SwarmLens backend — full build roadmap

Starting point: three-node gossip cluster (`store.py`, `gossip.py`, `main.py`),
plus worker leases (`worker.py`) — both built and verified. This document
covers everything left to reach a fully working backend.

Each phase lists: what to build, which files change, the concept it
demonstrates, and a concrete test that proves it works. Do them in order —
each one depends on the log/gossip foundation from Phase 0.

---

## Phase 0 — done

Event log (`events` table, append-only, `(origin, seq)` primary key),
gossip anti-entropy (push-pull via version vectors), CRDT reads (like
counts via distinct-guest counting), chaos endpoints (`/chaos/partition`,
`/chaos/heal`).

Verified: photo written to node1 reaches node3 within one gossip round;
likes cast during a partition converge with none lost after healing.

## Phase 1 — done

Worker leases (`worker.py`). Jobs claimed with an 8s lease, renewed by
heartbeat while working, reclaimed by any node if the claimant dies.

Verified: `kill -9` on the claiming node's process → job reclaimed →
exactly one `job_done` event across surviving nodes.

---

## Phase 2 — done

Raft leader election (`raft.py`, wired into `main.py`). Hand-rolled, no
library. One deviation from the numbers below worth flagging: heartbeat
interval is 50ms, not the 1s originally specified — 1s heartbeat with a
150-300ms election timeout is backwards (followers would time out before
the leader's next heartbeat ever arrives), so heartbeat needed to be well
under the timeout, not over it. Also found and fixed a real bug during
testing: the per-RPC httpx timeout must be well under the election
timeout too, or `asyncio.gather` waiting on a dead peer stalls every
election round long enough for the other candidate to steal the race —
a permanent two-node livelock, not just a slow election.

Verified: three nodes converge on one leader (`GET /raft/status`).
`kill -9` on the leader → new leader elected in 0.28-0.49s across
repeated trials (budget was ~1s). `recap_sent` (via `POST
/recap/trigger`, gated on `raft.role == "leader"`) fires exactly once
cluster-wide even when triggered again after the crash and re-election.
See `test_raft.py`.

<details>
<summary>Original spec</summary>

## Phase 2 — Raft leader election

**Why you need it.** Leases give you at-least-once, safe for idempotent
work like photo enhancement. Some actions are not naturally idempotent —
sending the "event recap ready" push notification, or triggering a
one-time highlight reel. Those need exactly one node acting, not three.

**What to build.** A new file `raft.py`. Minimum viable Raft — leader
election and heartbeats only, no log replication (your event log +
gossip already handles replication; don't duplicate that mechanism).

State machine per node:
```
FOLLOWER  --election timeout, no heartbeat--> CANDIDATE
CANDIDATE --wins majority vote-->              LEADER
CANDIDATE --sees higher term-->                FOLLOWER
LEADER    --sees higher term from anyone-->    FOLLOWER
```

Core fields: `current_term`, `voted_for`, `role`, `election_deadline`.

Endpoints to add in `main.py`:
- `POST /raft/request_vote` — `{term, candidate_id}` → `{term, granted}`
- `POST /raft/heartbeat` — `{term, leader_id}` → `{term, ok}`

Loop (in `raft.py`, started in `lifespan` like `gossip` and `worker`):
- If leader: send heartbeat to all peers every 1s.
- If follower/candidate: if no heartbeat received within a randomized
  150–300ms timeout, increment term, vote for self, request votes from
  all peers. Majority (2 of 3) → become leader.
- Randomized timeout is what prevents every node from becoming a
  candidate simultaneously and splitting the vote forever.

**Where it plugs in.** In `main.py`, gate the recap trigger:
```python
if raft.role == "leader":
    await send_event_recap()
```
Only the leader ever executes this branch.

**Test.** Start three nodes, confirm one becomes leader (`GET /raft/status`
returns role). `kill -9` the leader's process. Confirm a new leader is
elected within ~1s. Confirm the recap fires exactly once by counting a
`recap_sent` event across all surviving nodes' logs — same pattern as
the job_done check in Phase 1.

**Time budget.** This is the hardest phase. Budget 4–5 days, not 2.
If it's eating your schedule, `pysyncobj` (a Raft library) is a
legitimate substitute — implement election yourself, but say clearly
in your report which parts are library vs. hand-rolled.

</details>

---

## Phase 3 — done

Consistent hashing (`hashing.py`, wired into `main.py`'s `/zones`,
`/zones/local`, `/zones/ring`). Each zone is owned by one node on the
ring; `GET /zones` proxies each zone to its owner and only falls back to
this node's own replica (marked `stale: true`) if the owner doesn't
answer.

One bug found and fixed during testing, not in the original spec below:
a node's own ring identity must be the URL its peers already reach it at
(`SELF_URL`, new required env var), not its `NODE_ID`. The spec's
pseudocode used `gossip.alive_peers() + self` without saying what "self"
should be — self was originally `NODE_ID` while peers are addressed by
URL, which are different string spaces, so every node computed a
*different* ring for the same cluster and `/zones` gave a different
owner for the same zone depending which node you asked. Confirmed this
live: `bar`'s owner disagreed between node1 and node2 until `SELF_URL`
was added, after which `GET /zones/ring` matched byte-for-byte across
all three nodes.

Verified: three nodes, five zones, `/zones/ring` returns an identical
zone→owner mapping from all three. `/zones` returns identical scores
with `stale: false` on every zone (each is either local or a live
proxy). `kill -9` the node owning a zone → that zone's response flips to
`stale: true` immediately (the proxy call fails before gossip's 3-strike
failure detector evicts the peer) while every other zone stays fresh;
once gossip does evict the dead peer a few seconds later, the ring
reassigns that zone to a live node and `stale` goes back to `false`.
`test_hashing.py` is a pure unit test (no running nodes) confirming
adding a 4th node remaps ~23% of 200 zones, in line with the ~1/4
expected from the spec.

<details>
<summary>Original spec</summary>

## Phase 3 — Consistent hashing for zone ownership

**Why.** Right now every node computes zone scores from its own full
replica — fine at 200 photos, wasteful at scale, and it doesn't
demonstrate partitioning. Assign each venue zone to one owning node.

**What to build.** New file `hashing.py`:
```python
import hashlib

VIRTUAL_NODES = 100

def ring_for(members: list[str]) -> list[tuple[int, str]]:
    points = []
    for m in members:
        for v in range(VIRTUAL_NODES):
            h = int(hashlib.md5(f"{m}#{v}".encode()).hexdigest(), 16)
            points.append((h, m))
    return sorted(points)

def owner_of(key: str, ring: list[tuple[int, str]]) -> str:
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    for point_hash, member in ring:
        if h <= point_hash:
            return member
    return ring[0][1]   # wrap around
```

`members` = currently alive nodes, from `gossip.alive_peers()` plus self.
Recompute the ring whenever membership changes.

**Where it plugs in.** `GET /zones` only returns *authoritative* scores
for zones this node owns (`owner_of(zone, ring) == self.node_id`); for
others it proxies to the owning node or returns a cached/replicated
copy with a `stale: true` flag.

**Test.** Three nodes, several zones. Print zone → owner mapping. Add a
fourth node. Recompute the ring, print the mapping again, count how many
zones changed owner. With virtual nodes this should be roughly 1/4, not
all of them — that's the number for your report.

</details>

---

## Phase 4 — Quorum reads (the CAP knob)

**Why.** Makes the consistency/availability tradeoff a parameter you can
demo turning, instead of a fact you assert in the report.

**What to build.** Add `W` and `N` config to `main.py` (`N=3` fixed for
your cluster size), and a `/zones/quorum` endpoint that:
1. Queries `R` nodes (including self) for their zone scores.
2. Takes the response with the highest `event_count` per zone as freshest
   (or merges — since scores are derived from CRDT counts, you can just
   union the underlying like/photo events from all R responses and
   recompute, which is more honest than "highest wins").
3. Returns the merged result plus which nodes were actually queried.

Expose `R` and `W` as query params so you can demo `?R=1` vs `?R=2` live.

**Test.** Partition one node. With `R=2`, reads are always fresh (majority
excludes the stale node). With `R=1`, sometimes you land on the
partitioned node and get stale data — show this live, then heal and show
it self-corrects.

---

## Phase 5 — Guest client (offline queue + vector clocks)

**Why.** This is the other half of the system — without it you only have
a backend demo, not a product demo.

**What to build.** React + TypeScript PWA, separate repo or `/client`
folder.

- `Dexie.js` table `outbox`: `{local_id, kind, payload, vclock, synced}`
- On photo capture: write to `outbox` immediately, render optimistically,
  attempt POST to the nearest edge node in the background.
- Vector clock: each device keeps `{device_id: counter}` in `localStorage`
  (this one's fine as browser storage — it's small and disposable),
  increments on every local event, attaches the current clock to every
  event sent to a node.
- Service worker: `Background Sync API` retries the outbox whenever
  connectivity returns, without you polling manually.

**Where it plugs in.** `store.py`'s `events` table gains a `vclock`
column (JSON text). Concurrent events (neither vclock dominates the
other) render side-by-side in the gallery UI instead of being falsely
ordered by arrival time.

**Test.** Airplane-mode a phone, take 5 photos, confirm they queue
visibly in the UI. Reconnect. Confirm all 5 arrive at an edge node in
their original causal order, verified by inspecting the `vclock` field
in each event.

---

## Phase 6 — Cloud archive sync

**Why.** Post-event permanent gallery. Deliberately last and deliberately
optional — the system must be feature-complete without it.

**What to build.** A `sync_to_cloud` background task on whichever node is
Raft leader (using the leader election from Phase 2 — a natural second
use for it). Every N minutes, or triggered at event-end, push the full
event log to Supabase storage + Postgres. One-way, append-only, no reads
from Supabase during the live event.

**Test.** Disconnect the whole cluster from the internet. Confirm
uploads, likes, gossip, leases, and the zone map all continue working.
Reconnect — confirm the backlog syncs without needing you to do anything.

---

## Phase 7 — Chaos harness + metrics dashboard

**Why.** Your demo script needs one-click chaos, not curl commands typed
live under pressure.

**What to build.** A small HTML page (or a Vite React app) with buttons
that call the `/chaos/*` endpoints and `kill -9` via a tiny local control
API. Wire `/health` from all three nodes into a Prometheus scrape config,
Grafana dashboard showing: event count per node, gossip round count,
raft role per node, active leases, convergence lag (max digest
difference across nodes).

**Test.** The dashboard visibly reacts within 1–2 seconds of every chaos
button press. This is what's on the projector during your demo.

---

## Phase 8 — Load test and report numbers

**Why.** This is what separates a working demo from a graded result.

**What to build.** A `locust` or plain `asyncio` + `httpx` script that
fires 200 concurrent photo uploads and 500 concurrent likes at random
nodes, then measures:

- p50/p95/p99 upload latency
- convergence time (max digest lag over time, sampled every 100ms)
- recovery time after a node kill (time from kill to job reclaim to
  job_done)
- leader re-election latency (20 trials, kill leader each time)
- throughput at N=1,2,3,4 nodes (add Phase 3's hashing to test this)
- bandwidth used by gossip per round (log payload sizes)

Graph all of these. This is the difference between "it works" and "here
is why it works, quantified."

---

## Suggested pacing (8 weeks, solo, from here)

| Week | Phase |
|---|---|
| 1 | Phase 2 (Raft) — start early, it's the long pole |
| 2 | Phase 2 continued + Phase 3 (hashing) |
| 3 | Phase 4 (quorum) + Phase 5 start (client scaffold) |
| 4 | Phase 5 (offline queue, vector clocks, sync) |
| 5 | Phase 5 finish + on-device AI composition score (small) |
| 6 | Phase 6 (cloud sync) + Phase 7 (chaos harness) |
| 7 | Phase 7 finish + Phase 8 (load test) |
| 8 | Fix whatever the load test breaks, rehearse demo, write report |

If Phase 2 overruns, it's safe to cut Phase 6 (cloud sync) entirely —
the live system doesn't need it to be a complete distributed-systems
demo, it's a nice-to-have for the "permanent gallery" story only.

## What NOT to add

No new distributed mechanism beyond what's listed here. Every additional
protocol is additional failure-mode surface to debug under deadline
pressure. If you finish early, spend the extra time on Phase 8
(measurement) and on-device AI polish — not on a fourth consensus
algorithm.
