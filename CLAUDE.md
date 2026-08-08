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
  Each event also carries a `vclock` (JSON, envelope metadata like
  origin/seq — a guest device's `{device_id: counter}` clock, `{}` for
  internally-generated events with no device behind them) and `main.py`'s
  `GET /photos` uses it to compute `concurrent_with` per photo (see
  `_concurrent`/`_vclock_leq`) instead of implying a false total order
  from arrival time. `get_meta`/`set_meta` are node-local scratch state
  (like `local_seq`) — never gossiped, safe only for values it's fine to
  lose or recompute on a fresh process (`cloud_sync.py`'s checkpoint uses
  this — see its entry below for why that's still correct).
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
  nodes at random, unions their raw photo/like/aesthetic_score events
  (`store.raw_events` / `GET /zones/events`) deduped on `(origin, seq)`,
  and recomputes scores from the merge. This is the CAP-tradeoff knob:
  `R` over `N/2` is always fresh, `R=1` can land on a stale/partitioned
  node.
- `ai_engine.py` — pretrained-only photo analysis (no training anywhere):
  saliency subject detection, a rule-of-thirds AR reframe guide, CLIP
  film-stock suggestion, and a CLIP-embedding aesthetic score. Wired up
  as `POST /analyze` in `main.py`, which runs it on a thread (it's
  synchronous CPU-bound ONNX/OpenCV work — awaiting it directly would
  stall this node's asyncio loop, and gossip/raft both depend on that
  loop staying responsive). Models are downloaded on first use into
  `./models/` (gitignored, ~154MB) and cached after that:
  - saliency: `cv2.saliency.StaticSaliencySpectralResidual` — ships in
    `opencv-contrib-python`, no download, classical CV.
  - filter suggestion + aesthetic embedding: CLIP ViT-B/32, int8-quantized
    ONNX export by Xenova of `openai/clip-vit-base-patch32`
    (huggingface.co/Xenova/clip-vit-base-patch32).
  - aesthetic score: LAION `aesthetic-predictor` v1's ViT-B/32 linear
    head (github.com/LAION-AI/aesthetic-predictor,
    `sa_0_4_vit_b_32_linear.pth`) — the *original* repo, not
    "improved-aesthetic-predictor" (that one is ViT-L/14 only, wrong
    embedding dimension for our ViT-B/32 CLIP). See `ai_engine.py`'s
    module docstring for the full reasoning and exact URLs.
  - the `aesthetic_score` event feeds `avg_aesthetic` into `/zones` and
    `/zones/quorum` the normal way (`store.append_local`, replicated by
    gossip) — see the Conventions section, no second source of truth.
- `cloud_sync.py` — leader-gated background loop (`POST
  /cloud_sync/trigger`, `GET /cloud_sync/status` in `main.py`, folded
  into `GET /health`) that pushes the event log to a Supabase/PostgREST
  -style endpoint, one-way and append-only. Disabled entirely (loop never
  starts) unless `SUPABASE_URL` is set — zero effect on any node that
  doesn't configure it. Reuses `store.events_missing_from()` — the exact
  primitive `gossip.py` already uses — treating "the cloud" as one more
  peer whose digest is tracked, except sync only pushes. The local sync
  checkpoint (`store.get_meta`/`set_meta`) is deliberately *not*
  replicated or authoritative — see the Gotchas section for why that's
  still correct across a Raft leadership change, not just convenient.

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
- `python test_ai_engine.py` — spins up 3 nodes, posts synthetic images
  crafted to have an unambiguous right answer (an off-center circle for
  the AR guide, a sunset gradient vs. flat gray for filter suggestion),
  confirms the AR guide direction and that suggested_filter actually
  differentiates, then confirms the resulting `aesthetic_score` event
  count matches on all three nodes' own `.db` files after a gossip
  round. First run downloads ~154MB of models into `./models/`
  (gitignored) — cached after that, budget extra time for a cold run.
- `python test_vclock.py` — spins up 3 nodes, simulates a couple of
  guest "devices" attaching `{device_id: counter}` clocks directly to
  `POST /photos` (no real guest client exists to generate these yet),
  confirms `concurrent_with` matches the actual causal structure (mutual
  concurrency for independent devices, no false concurrency once one
  clock causally dominates another, never concurrent for a bare photo
  with no vclock), and confirms it all survives gossip replication.
- `python test_cloud_sync.py` — spins up 3 nodes plus a tiny in-process
  fake-cloud HTTP server (no real Supabase project exists for this repo)
  that dedups on `(origin, seq)` the way a real unique constraint would.
  Confirms the cluster keeps working while the fake cloud isn't
  listening ("no internet"), `/cloud_sync/trigger` fails softly instead
  of 500ing, the full backlog syncs in one push once it starts listening
  ("reconnect"), a no-op re-trigger creates no duplicates, a new event
  afterward syncs incrementally, and only the Raft leader ever pushes.
- No test file yet for gossip/worker phases — those were verified
  manually per the curl sequences in README.md. Same for the live
  `/zones` ownership/proxy/stale-fallback behavior — verified manually
  (see the Phase 3 writeup in ROADMAP.md for what was checked).

## Current status

Phases 0-4 done (gossip replication, worker leases, Raft election,
consistent-hash zone ownership, quorum reads), the AI analysis engine
(`ai_engine.py`, `POST /analyze`), Phase 5's backend slice (vector clocks
+ `concurrent_with`), and Phase 6 (cloud archive sync, `cloud_sync.py`)
described above. See ROADMAP.md for the full phase list. Not started:
the guest client itself (Dexie.js outbox, localStorage vector clock,
service worker -- Phase 5's other half), the guest UI and operator
console wiring, Phase 7 (chaos/metrics dashboard), Phase 8 (load test)
-- the guest UI and operator console repos aren't present in this
working directory, so that wiring can't happen from here until they're
attached alongside the backend.

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
- **...but a repeated, ongoing sync (not a one-time trigger) can safely
  use a local, non-replicated checkpoint, IF the destination is itself
  idempotent.** `cloud_sync.py`'s "what has the cloud already got"
  checkpoint lives in `local_meta` (`store.get_meta`/`set_meta`) —
  node-local, never gossiped, gone if that node dies. That's fine here,
  unlike the recap case above: if Raft leadership moves to a different
  node, the new leader's checkpoint doesn't know what the old leader
  already pushed, so it just resends that overlap — and the Supabase
  table's `UNIQUE(origin, seq)` constraint (`Prefer:
  resolution=ignore-duplicates`) silently drops it. Correct, if wasteful
  once. The dividing line: gate a *single, one-time* action on the
  replicated log (recap); a *repeated, resumable* push can trust local
  state as long as the far end dedups on the same key gossip already
  uses.
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
- **Any synchronous, CPU-bound call (ONNX inference, OpenCV, ...) in a
  route handler must be offloaded with `asyncio.to_thread`, never
  awaited directly.** This process has exactly one asyncio event loop,
  and gossip's anti-entropy round and raft's 50ms heartbeat/election
  timers both run as tasks on it. A multi-second synchronous call inside
  a request handler (e.g. `POST /analyze`, see `ai_engine.py`) blocks
  that whole loop for its duration — on any node still holding
  leadership, that's long enough to miss heartbeats and get voted out by
  a follower that never actually lost contact.
- **Aesthetic-predictor checkpoints are keyed to a specific CLIP
  backbone's embedding dimension, and the commonly-cited repo only
  covers one of them.** LAION's "improved-aesthetic-predictor" ships
  ViT-L/14 (768-dim) heads only; using it with ViT-B/32 (512-dim)
  embeddings is a shape mismatch, not a "close enough" approximation.
  The *original* `LAION-AI/aesthetic-predictor` repo has both `vit_b_32`
  and `vit_l_14` linear heads — `ai_engine.py` uses the B/32 one to
  match the B/32 CLIP backbone used for filter suggestion (so the
  aesthetic head can reuse that embedding instead of re-running a second,
  larger CLIP model just for one score).

## Conventions

- Every derived read (photo list, zone scores, job status, like counts)
  is computed by replaying `events`, never stored redundantly. If you add
  a new feature, follow this pattern rather than adding a second source
  of truth.
- No new distributed-systems mechanism beyond what ROADMAP.md lists.
  Every extra protocol is extra failure-mode surface — see "What NOT to
  add" at the bottom of ROADMAP.md.
