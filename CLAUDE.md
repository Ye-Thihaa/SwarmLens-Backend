# SwarmLens backend

Three identical FastAPI nodes (`main.py`), each with its own SQLite file —
no shared database, no central authority. State converges by gossip.
Full design and phase-by-phase plan: [ROADMAP.md](ROADMAP.md). Endpoint
list and manual demo commands: [README.md](README.md).

## Files

- `main.py` — FastAPI app + all HTTP endpoints. Same file runs as every
  node; behavior is set entirely by env vars (`NODE_ID`, `DB_PATH`, `PEERS`).
- `store.py` — SQLite-backed append-only event log. All state (photos,
  likes, job leases, recap) is derived by replaying `events`, never
  written directly. `(origin, seq)` primary key makes merges idempotent.
- `gossip.py` — anti-entropy loop: every `GOSSIP_INTERVAL` (default 1s),
  pick one random peer, exchange version-vector digests, pull/push
  whatever's missing.
- `worker.py` — claims unclaimed jobs with an 8s lease, renews by
  heartbeat, another node reclaims it if the claimant dies. At-least-once,
  fine for idempotent work (photo enhancement).
- `raft.py` — leader election only (no log replication — the event log +
  gossip already do that). Used for exactly-once actions where at-least
  -once isn't safe (e.g. the one-time event recap notification).
- `hashing.py` — consistent-hash ring (100 virtual nodes per member) over
  the currently alive cluster. `GET /zones` in `main.py` uses it to route
  each zone to its one owning node instead of every node scoring every
  zone from its own replica.
- `GET /zones/quorum` (in `main.py`, no separate file — it's short
  enough to live next to the other zone endpoints) — samples `R` of `N`
  nodes at random, unions their raw photo/like events (`store.raw_events`
  / `GET /zones/events`) deduped on `(origin, seq)`, and recomputes
  scores from the merge. This is the CAP-tradeoff knob: `R` over `N/2`
  is always fresh, `R=1` can land on a stale/partitioned node.

## Run it

Three terminals, from this directory:

```bash
NODE_ID=node1 DB_PATH=./node1.db SELF_URL=http://127.0.0.1:8001 PEERS=http://127.0.0.1:8002,http://127.0.0.1:8003 uvicorn main:app --port 8001
NODE_ID=node2 DB_PATH=./node2.db SELF_URL=http://127.0.0.1:8002 PEERS=http://127.0.0.1:8001,http://127.0.0.1:8003 uvicorn main:app --port 8002
NODE_ID=node3 DB_PATH=./node3.db SELF_URL=http://127.0.0.1:8003 PEERS=http://127.0.0.1:8001,http://127.0.0.1:8002 uvicorn main:app --port 8003
```

`SELF_URL` must be the address peers reach this node at (i.e. it's the
one that appears in *their* `PEERS` lists) — see the Gotchas section.

Or `docker compose up --build` (nodes land on :8001-8003).

Tests:
- `python test_raft.py` — spins up 3 nodes itself, confirms leader
  election, `kill -9`s the leader, confirms re-election + exactly-once
  recap. Self-contained, cleans up its own processes and `.db` files.
- `python test_hashing.py` — pure unit test of `hashing.py`'s ring, no
  running nodes needed. Confirms the mapping is deterministic and that
  adding a 4th node to a 3-node ring remaps ~1/4 of zones, not all of
  them.
- `python test_quorum.py` — spins up 3 nodes, fully (bidirectionally)
  partitions one, confirms `R=1` quorum reads sometimes return stale
  data from the partitioned node while `R=2` never does, then heals and
  confirms `R=1` goes back to reliably fresh. Self-contained like
  `test_raft.py`.
- No test file yet for gossip/worker phases — those were verified
  manually per the curl sequences in README.md. Same for the live
  `/zones` ownership/proxy/stale-fallback behavior — verified manually
  (see the Phase 3 writeup in ROADMAP.md for what was checked).

## Current status

Phases 0-4 done (gossip replication, worker leases, Raft election,
consistent-hash zone ownership, quorum reads). See ROADMAP.md for the
full phase list and what's next (Phase 5: guest client with offline
queue + vector clocks).

## Gotchas hit so far (don't reintroduce these)

- **Raft heartbeat must be well under the election timeout, not over
  it.** The original spec numbers (150-300ms election timeout, 1s
  heartbeat) were backwards — a follower would time out before the
  leader's next heartbeat ever arrived. Heartbeat is 50ms in `raft.py`.
- **Per-RPC timeout in `raft.py` must also be well under the election
  timeout.** `asyncio.gather` waits for every peer including dead ones;
  if a single dead-peer connection attempt takes close to (or longer
  than) the election timeout to fail, every election round stalls long
  enough for a competing candidate to win the race first — a permanent
  two-node livelock, not just a slow election. Keep `RPC_TIMEOUT` short
  (currently 50ms).
- **Exactly-once actions gated on `raft.role == "leader"` must not
  double-fire across a leader crash.** Don't tie the action to the
  moment of *becoming* leader (that fires on every election, including
  ones after the action already happened). Instead, gate an explicit
  one-time trigger and make the action itself idempotent by checking the
  replicated event log (see `store.event_exists` / `send_event_recap` in
  `main.py`), not local in-memory state — local state doesn't survive
  the crash and isn't shared across nodes.
- **A node's ring identity must match how its peers already address it,
  not `NODE_ID`.** Peers only know each other by the URLs in `PEERS` —
  they never learn each other's `NODE_ID`. If a node computed its own
  ring membership using `NODE_ID` while every peer's entry was a URL,
  each node ends up hashing a *different* set of member strings for the
  same physical cluster, so `owner_of(zone, ring)` disagreed node to
  node — confirmed live, `bar`'s owner differed between node1 and node2.
  Fixed with `SELF_URL` (`main.py`'s `SELF_ID`), which must be set to the
  address this node's peers reach it at. If you add another feature that
  builds a member list from `gossip.alive_peers()` plus self, use
  `SELF_ID`, not `NODE_ID`.
- **`POST /chaos/partition/{i}` only stops *this* node from initiating
  gossip toward `PEERS[i]` — it does not block the receiving end.** A
  partitioned peer can still successfully call *your* `/gossip/sync` and
  `/gossip/push` and pull/push data that way, so a one-sided partition
  self-heals within a gossip round or two instead of holding. To
  actually isolate a node for a test (see `test_quorum.py`), partition
  it in both directions from every other node: each of the other nodes
  must stop initiating toward it, *and* it must stop initiating toward
  each of them — 4 calls total to isolate one node in a 3-node cluster,
  not 1.

## Conventions

- Every derived read (photo list, zone scores, job status, like counts)
  is computed by replaying `events`, never stored redundantly. If you add
  a new feature, follow this pattern rather than adding a second source
  of truth.
- No new distributed-systems mechanism beyond what ROADMAP.md lists.
  Every extra protocol is extra failure-mode surface — see "What NOT to
  add" at the bottom of ROADMAP.md.
