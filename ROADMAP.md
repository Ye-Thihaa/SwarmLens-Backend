# SwarmLens backend — full build roadmap

Starting point: three-node gossip cluster (`store.py`, `gossip.py`, `main.py`),
plus worker leases (`worker.py`) — both built and verified. This document
covers everything left to reach a fully working backend.

Each phase lists: what to build, which files change, the concept it
demonstrates, and a concrete test that proves it works. Do them in order —
each one depends on the log/gossip foundation from Phase 0.

## AI analysis engine — done (outside the phase numbering)

`ai_engine.py` / `POST /analyze` — saliency-based AR reframe guide, CLIP
film-stock suggestion, and a CLIP + LAION-linear-head aesthetic score, all
pretrained, nothing trained here. This isn't one of the numbered phases
below (it's the "new piece" from the project proposal, feeding the
`aesthetic_score` event that the zone CRDT in §5.3 depends on), so it's
called out separately rather than slotted into the phase order. Full
writeup, model sources/versions, and the live demo: see README.md's "AI
analysis engine" section and CLAUDE.md's Files list. Tested via
`test_ai_engine.py` against 3 live nodes.

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
answer. Each zone's score also carries `avg_aesthetic` (added when the AI
engine landed, after this phase was first built — null until at least
one photo in that zone has been through `POST /analyze`).

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

## Phase 4 — done

Quorum reads (`GET /zones/quorum?R=&W=`, in `main.py`). `N` is fixed at
`len(PEERS) + 1`. Each call samples `R` of `N` members at random
(self is just one candidate among them, not privileged), fetches raw
photo/like/aesthetic_score events from each via the new `GET
/zones/events`, unions them deduped on `(origin, seq)` — the same
idempotence key gossip already relies on — and recomputes zone scores
from the merge, rather than trusting any single node's already
-aggregated counts. `W` is accepted and echoed back for the
CAP-tradeoff narrative only: this system's writes are always local +
eventually gossiped, so there's no synchronous quorum write path for
`W` to actually gate. (`aesthetic_score` was added when the AI engine
landed, after Phase 4 was first built — see the AI engine section above.)

One thing the spec's pseudocode glossed over: sampling has to be
*random* on every call for "sometimes you land on the stale node" to be
an observable, repeatable demo rather than a coin flip you assert
happened. `/zones/quorum` re-samples `R` members fresh each request.

Verified (`test_quorum.py`, spins up and fully partitions its own
3-node cluster): with node3 bidirectionally isolated and a write made
after the partition, `R=1` against node1 lands on the stale node often
enough to show real undercounts (21 fresh / 9 stale over 30 tries) while
`R=2` was fresh 20/20 — any 2-of-3 sample is guaranteed to include a
node outside a single-node partition. After healing and letting gossip
catch up, `R=1` went back to consistently fresh (10/10).

<details>
<summary>Original spec</summary>

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

</details>

---

## Phase 5 — done, including a guest client (functional scaffold, not the branded UI)

Backend half: `store.py`'s `events` table gained a `vclock` column (JSON
text, envelope metadata alongside origin/seq/kind, not payload).
`POST /photos` and `POST /likes` accept an optional `vclock: {device_id:
counter}`; events created internally (job leases, recap, aesthetic_score)
default to `{}` since there's no guest device behind them. `GET /photos`
computes `concurrent_with` per photo: other photo_ids whose vclock
neither dominates nor is dominated by this one, so a gallery UI can
render them side by side instead of implying a false total order from
arrival time. An empty vclock (no client attached one) is never flagged
concurrent with anything; that would assert causal knowledge that
doesn't exist. Verified via `test_vclock.py` (simulated devices) --
see below for the same mechanism verified again through a real client.

Guest half: `client/` -- React 19 + TypeScript PWA (Vite), Dexie.js
`outbox` table, `localStorage` vector clock, a service worker with a
real Background Sync handler (`vite-plugin-pwa`, `injectManifest`
strategy). This is a functional reference implementation proving the
offline-queue + causal-ordering mechanism end to end, not the polished,
branded guest UI described elsewhere in the project's task list (AR
reframe overlay, film-stock auto-selection from `POST /analyze`) --
that's a separate frontend not attached to this working directory. See
`client/README.md` for the full stack rationale.

Verified live in a browser against the real 3-node cluster (not just
unit tests): shutter capture writes to the outbox and renders
optimistically; the sync engine picks a reachable node and POSTs with
the device's vclock attached, returning a real `photo_id`; the gallery
renders this device's own synced photos with their real local image
(this backend has no photo-binary storage of its own -- see
`ai_engine.py`'s `/analyze` docstring -- so only locally-captured photos
have real bytes available; everything else renders as a metadata-only
card); killing the backend mid-capture leaves the photo visibly queued;
restarting the backend and waiting -- no manual action -- the periodic
foreground retry picks it up automatically and syncs it; and three
photos posted with mutually non-dominating vector clocks rendered as one
concurrent cluster with a badge, not three separately-ordered items --
the same `_concurrent` logic from `main.py`, now exercised through a
real client instead of a simulated one.

**Cross-browser note on Background Sync:** the spec's "Background Sync
API retries the outbox whenever connectivity returns, without you
polling manually" is real (`client/src/sw.ts`'s `sync` event handler),
but Background Sync API is Chromium-only -- no Safari, no Firefox. The
primary, universally-supported retry path is a foreground `online`
listener + a 10s poll (`OutboxStatus.tsx`); Background Sync is a
progressive enhancement on top of that (retries even with the tab
closed, on browsers that support it), not the only mechanism.

<details>
<summary>Original spec</summary>

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

</details>

---

## Phase 6 — done

`cloud_sync.py`, wired into `main.py` (`POST /cloud_sync/trigger`, `GET
/cloud_sync/status`, folded into `GET /health`). A background loop
(`CLOUD_SYNC_INTERVAL`, default 15s) that only acts when `raft.role ==
"leader"` -- using the leader election from Phase 2, exactly as the spec
suggested. Disabled entirely (loop never even starts) unless
`SUPABASE_URL` is set, so it has zero effect on any node that doesn't
configure it -- including every other test in this repo.

The interesting design choice: rather than inventing a new sync
mechanism, it reuses `store.events_missing_from()` -- the exact primitive
gossip already uses for anti-entropy -- treating "the cloud" as one more
peer whose digest gets tracked, except the sync only pushes (no
pull-back). The local "what's the cloud already got" checkpoint lives in
`local_meta` (`store.get_meta`/`set_meta`, new this phase) and is
explicitly *not* replicated or treated as authoritative: if Raft
leadership moves to a different node, the new leader's checkpoint won't
know what the old leader already pushed, so it just resends from
scratch and the destination's own `UNIQUE(origin, seq)` constraint (+
`Prefer: resolution=ignore-duplicates`, PostgREST's built-in dedup)
silently drops the overlap. Correct, if wasteful once -- the same lesson
as CLAUDE.md's recap gotcha: gate exactly-once behavior on the
destination's idempotency, not on local state that can't survive a
leadership change.

No real Supabase project exists for this repo, so it's config-driven
against any PostgREST-style endpoint (`SUPABASE_URL`, `SUPABASE_KEY`,
`SUPABASE_EVENTS_TABLE`) rather than hardcoded -- point it at a real
Supabase project by setting those three env vars once a table with the
right schema and unique constraint exists (see `cloud_sync.py`'s module
docstring for the exact column list).

Verified via `test_cloud_sync.py` against a tiny in-process fake-cloud
HTTP server (dedups on `(origin, seq)` the same way a real unique
constraint would) standing in for Supabase: confirmed uploads, gossip,
and `/health` all keep working while the fake cloud isn't listening yet
("no internet"); confirmed `/cloud_sync/trigger` fails softly (`synced:
0`, `last_error` set) rather than 500ing; confirmed the full backlog
syncs in one push once the fake cloud starts listening ("reconnect");
confirmed a no-op re-trigger pushes nothing and creates no duplicates;
confirmed one new event afterward syncs incrementally, not a full
resend; confirmed a follower's trigger is always a no-op.

<details>
<summary>Original spec</summary>

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

</details>

---

## Phase 7 — done (lightweight alternative, not Prometheus/Grafana)

`dashboard.html`, served at `GET /dashboard` by every node identically
(`main.py`). Self-contained static HTML/CSS/vanilla JS, no build step, no
external dependencies -- polls `GET /health` (which now also carries
`raft` and `cloud_sync` status) on all 3 nodes every 1s and renders: role
badge, term, current leader, event count, gossip rounds/pulled/pushed,
cloud-sync status, and which peers this node has an active outgoing
partition against. Buttons drive the existing `/chaos/partition/{i}` and
`/chaos/heal` endpoints, plus a convenience "Isolate this node" button
that fires all 4 directional partition calls needed for a *real*
bidirectional isolation in one click (see the CLAUDE.md gotcha on why a
single-direction partition alone self-heals within a round or two).

The spec's original text offered a choice -- full Prometheus/Grafana, or
a lighter alternative -- and said to ask before building either. Since
this session had authorization to push the backend through to completion
and no operator-console repo is attached here to integrate a heavier
setup into anyway, the lighter alternative was the only one actually
buildable and deliverable from this working directory, so that's what
got built rather than blocking on the question. If the real
Prometheus/Grafana stack is still wanted later, `GET /health` already
exposes everything a scrape config would need -- no backend changes
required to add that on top.

**No real kill -9 button.** The spec's "kill -9 via a tiny local control
API" needs an HTTP endpoint that can execute an OS-level process kill --
a genuinely different risk profile from `/chaos/partition`, which just
flips an in-memory flag. Exposing "kill an arbitrary process" over an
open, unauthenticated HTTP API (this backend has no auth anywhere) is a
real security surface, not just scope creep, so it was deliberately left
out. Killing a node for a demo is one `kill -9 <pid>` in a terminal --
see `test_raft.py` for exactly that, done programmatically. If a
proper operator console gets built later, whether ITS kill button should
shell out for real or be a rehearsal-mode visual toggle is a decision
for that build, not resolved here.

Verified live in a browser (not just curl): loaded `/dashboard`, drove
`isolate()`/`healAll()`/`partition()` and confirmed the UI reflected each
change within the next 1s poll tick, and killed a node's process
out-of-band to confirm it renders as "unreachable" once the poll catches
up. Caught and fixed a real bug this way that a curl-only check would
have missed: every button's `onclick` called `byId('nodeX')` as a
function, but `byId` is a lookup object, not a function -- every single
chaos button was broken until this was caught by actually clicking
through it.

<details>
<summary>Original spec</summary>

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

</details>

---

## Phase 8 — done

`load_test.py`, plain `asyncio` + `httpx` (no locust, no new
dependency). Fires real traffic at real clusters it spins up and tears
down itself -- never mocks. `python load_test.py --quick` runs a fast,
reduced-scale version of every measurement to validate the script;
without `--quick` it runs the full spec'd numbers (200 uploads, 500
likes, 20 election trials), which takes a few minutes end to end.
Outputs `results.json` + `summary.csv` + 6 PNGs into
`load_test_results/` (gitignored -- results from one run on one
machine, not source).

**A real, load-test-only bug was found and fixed before any numbers
could be trusted.** `store.py`'s `_next_seq()` reads the local sequence
counter, increments it in Python, then writes it back -- a read-modify
-write spanning an `await`. Every prior test in this repo only ever
awaited one write at a time, so this never mattered. The load test's
first run threw `sqlite3.IntegrityError: UNIQUE constraint failed:
events.origin, events.seq` under real concurrency: two requests both
read the same counter value before either wrote back, assigned the same
`seq`, and collided. Fixed with an `asyncio.Lock` around the whole
"assign next seq + insert" critical section in `append_local`
(`merge_remote` doesn't need it -- it inserts events whose `(origin,
seq)` were already assigned by their origin node, via `INSERT OR
IGNORE`, safe under concurrency on its own). 200 concurrent uploads at
one node: 0 failures after the fix, reliably reproducing before it.

**A second methodology bug, in the load test itself, not the backend:**
the first version of the recovery measurement polled only one node to
detect "claimed", then killed the claimant. Since a node's own writes
are visible to itself instantly but to an *observer* only after the
next gossip round, that one-node-observer approach could detect the
claim late enough that the claimant finished its whole 5-second
fake-work window before the kill signal landed -- measuring "did the
original worker finish before I managed to kill it" (5.0s, suspiciously
exactly the fake-work duration) instead of a genuine kill-mid-work
scenario. Fixed by polling all 3 nodes directly and in parallel, so the
claim is caught via the claimant's own zero-lag self-report. After the
fix: a clean, explainable 13.25s (8s lease expiry + 5s fake work +
overhead) -- matching `worker.py`'s own `LEASE_SECONDS`/`work_seconds`
constants almost exactly, which is exactly the kind of number that
means the measurement is trustworthy, not lucky.

**Numbers from a full run** (this machine, one run -- see
`load_test_results/summary.csv` for the raw numbers of any given run):

| Metric | Value |
|---|---|
| Photo upload latency (200 concurrent) | p50 1.39s / p95 1.59s / p99 1.60s |
| Like latency (500 concurrent) | p50 1.53s / p95 2.54s / p99 2.71s |
| Convergence time after the burst | 1.89s |
| Recovery (kill -> reclaim -> done) | 13.25s |
| Leader re-election (20 trials) | min 0.19s / mean 0.29s / max 0.85s |
| Throughput, N=1 / 2 / 3 / 4 | 81 / 132 / 164 / 220 uploads/sec |
| Gossip `/sync` payload | cold (full backlog) 112KB / warm (steady state) 509B |

**The interesting finding, not just the numbers:** upload/like latency
under the full 200/500-concurrent burst (~1.4-2.7s) is far higher than
under light load (~0.1-0.3s in `--quick` mode, 20/50 concurrent). This
is the direct, honest cost of the concurrency fix above: `_write_lock`
correctly serializes every write through one SQLite connection per
node, so under a genuine 200-way concurrent burst, request #200 queues
behind roughly 199 others. This is a real architectural ceiling (single
-writer SQLite per node), not a bug -- and throughput scaling with N
(81 -> 220 uploads/sec from N=1 to N=4) is the direct upside of the same
fact: each additional node is another fully independent SQLite writer,
so spreading writes across more nodes raises the cluster's aggregate
write ceiling almost linearly. This is exactly the "why it works,
quantified" the spec asked for, not just "it works."

---

## Branded guest app + operator console — done (outside the phase numbering)

`client-2/` — added mid-project as a Lovable-generated TanStack Start
scaffold (React 19, two separate apps sharing one design system: a
guest app and an operator console), with zero backend calls anywhere:
a static bundled JPEG stood in for the camera feed, and the operator
console's "kill the leader" / "partition" buttons just flipped local
React state. Wired the whole thing to the real 3-node cluster.

**Guest app**: real `getUserMedia` camera capture (the mock had none),
a real zone picker using the backend's actual zones (the mock had no
zone concept at all — capture just implied one via a hardcoded nudge
string), a real offline outbox (localStorage-backed — this app has no
Dexie, unlike `client/`'s Phase 5 IndexedDB one), real gallery/likes/
heatmap from `GET /photos` + `GET /zones`, and `POST /analyze` now
fires automatically after every upload — the first time either guest
client has actually exercised `ai_engine.py`'s aesthetic pipeline
instead of leaving it completely dark. Landing page collapses to one
real "room" card with live guest/frame counts, since this backend has
no multi-event concept to back the mock's fake 3-event directory.

**Operator console**: real Raft term/role and gossip/partition state
polled from all 3 nodes every second, a real event tape built from
*observed state diffs* (term changes, peer reachability flips) instead
of scripted log lines, and a real `GET /zones/quorum?R=&W=` demo
(sample size, `queried`/`unreachable`, `strongly_consistent`) replacing
the mock's fake "W=1 R=1 stale read" button. Chaos actions were
relabeled honestly rather than kept as-is: "Isolate the leader," not
"Kill the leader" — `POST /chaos/partition` only stops gossip
anti-entropy, and Raft's heartbeats bypass `gossip.partitioned`
entirely (same distinction as the existing Gotchas entry on this), so
isolating the leader demonstrates gossip/quorum staleness, not a
forced re-election. No real process-kill button was added, matching
`dashboard.html`'s own documented stance on why that was deliberately
left unbuilt.

**Auth, added after the fact at the user's request**: a real password
gate on `/console` (`client-2/src/lib/consoleAuth.ts`) — a TanStack
server function verifies the password server-side and issues an
HttpOnly session cookie, checked by the route's `loader` on every
request including the first SSR render, so an unauthenticated
visitor's HTML never contains the console's data. Chaos actions route
through server functions (`src/lib/operatorGateway.ts`) rather than
straight from the browser to the FastAPI backend, so `main.py`'s new
`OPERATOR_TOKEN` gate (`require_operator_token`, `/chaos/partition`
and `/chaos/heal` only, `secrets.compare_digest`, fails open when
unset so existing tests/`dashboard.html` keep working) never has to be
readable from client JS — only this Node process's own `process.env`
ever sees it.

**Three real bugs found wiring this up, in order:**

1. `useSyncExternalStore`'s snapshot function re-parsed the outbox's
   localStorage JSON on every call — a new array reference each time,
   which React reads as "always changed," looping the `EventGallery`
   route into "Maximum update depth exceeded" on every visit. Fixed by
   caching against the raw string and only re-parsing when it actually
   changed.
2. The `.env` loader for `CONSOLE_PASSWORD`/`OPERATOR_TOKEN` was first
   added to `src/start.ts`, which (unlike `src/server.ts`) is bundled
   for the *client* too — importing `node:fs`/`node:path` there broke
   hydration with a cryptic "Module externalized for browser
   compatibility" error. Moved to `server.ts`, the genuinely
   server-only Nitro entry point.
3. Vite's own internal `.env` loading (`dotenv-expand`) treats `$` as
   variable-interpolation syntax and silently mangled
   `CONSOLE_PASSWORD=c0n$ol3` down to `"c0n"` (`$ol3` resolved as a
   reference to an undefined env var and expanded to nothing) before
   the app's own loader ran — and that loader's "only set if not
   already present" guard then kept the mangled value. Fixed by making
   the custom loader unconditional so the literal file value always
   wins over whatever Vite already put there.

Also found, unrelated to the wiring itself: `test_vclock.py` (likely
every `test_*.py` here) hardcodes ports 8001-8003 and doesn't fail to
bind if those are already in use — it silently attaches to an
already-running demo cluster and posts its synthetic test events
(guest `g1`) straight into real data. Confirmed live: a test run left
a fake guest sitting in the actual gallery. See the Gotchas section in
CLAUDE.md for the operational rule this implies.

Verified live end-to-end in a browser: real photo capture → real
upload → real gallery display on a device that never captured it →
real like → real `/analyze` call → real operator-console login → a
real `/chaos/partition` isolate/heal cycle with the event tape logging
genuine state transitions → a real `R=1` quorum read landing on the
isolated node → confirmed the browser's network tab never once shows
a direct call to `/chaos/*` post-auth, only the `_serverFn` proxy.

---

## Live AI compose guidance — done (outside the phase numbering)

`POST /analyze/preview` + the AI compose overlay in
`client-2/src/routes/capture.tsx`. Moves `ai_engine.py`'s existing
analysis from *after* the shutter to *before* it, so the guest gets
composition help while framing instead of a verdict once the photo is
already taken.

Almost nothing new had to be invented on the model side — saliency,
CLIP film-stock matching and the aesthetic head were all already there,
and `/analyze` was already returning `ar_guide` and `suggested_filter`
to a client that read neither. What was missing was a crop rectangle
(`compute_reframe`), a scene colour read (`describe_scene`), a
recommendation sentence (`compose_reason`, templated from measured
values — no LLM, no new dependency), and film-stock keys that matched
the guest UI's actual strip, which finally cashed the placeholder TODO
`FILM_STOCKS` had carried since the engine was written.

**The one real design constraint:** `/analyze` appends an
`aesthetic_score` event, and a viewfinder calls this every 1.5s against
a frame that has no `photo_id` and was never shot. So the preview path
computes strictly more and persists strictly nothing. It's also
semaphore-bounded and sheds with 503 instead of queueing — guidance for
a frame the guest has already panned away from is worse than no
guidance. Measured at ~37ms per frame with `/health` staying under 10ms
alongside it, so raft's 50ms heartbeat is untouched.

**Four real bugs found by measuring rather than eyeballing:**

1. **One CLIP prompt per film stock produced a class prior that beat the
   signal.** Across 18 real reference frames, *one stock won all 18* —
   including near-black ones — because a long prompt full of generic
   photographic words carries a constant offset (+0.034 mean) larger
   than the per-image variation (0.02–0.056). Fixed by mean-centering
   the text embeddings and ensembling several short prompts per stock
   (CLIP's own zero-shot recipe). The same 18 frames then split across 4
   stocks, and the vivid yellow motorcycle picked the saturated stock —
   the same call the reference app made.
2. **A "blown highlight" mask (`V > 250`) discarded every pixel of a
   fully saturated image.** A bright orange sunset came back as *"dim,
   near-colorless light"* with mean saturation and brightness both
   exactly 0.0 — the mask had emptied the array. Saturation, not
   brightness, is the correct test for "no usable hue".
3. **Whole-image saliency statistics collapse to the whole image.** Both
   min/max and a 6th/94th percentile bbox returned a subject box
   covering ~90% of every real photo (foliage and gravel speckle
   saliency everywhere), which silently made the reframe a permanent
   no-op. Fixed with blur → threshold → morphology → strongest connected
   component, ranked on mean saliency × area.
4. **A featureless image produced a confident 4× zoom into an empty
   corner.** With nothing salient, Otsu picked up a handful of noise
   pixels and the reframe dutifully built a rectangle around them. Fixed
   with area bounds on what counts as a subject at all — below the floor
   it's speckle, above the ceiling it *is* the scene — and the UI draws
   nothing when `subject_found` is false.

5. **The subject detector inverted the guidance near a thirds line** —
   found only after the browser test "passed". Picking the strongest
   connected component (the fix for bug 3) breaks differently: spectral
   residual answers on *edges*, so one object splits into separate
   fragments and the winner's centroid sits off to one side, while the
   dim background that survives thresholding drags it toward frame
   centre. Synthetic subjects at x=0.22 and x=0.72 were reported at 0.40
   and 0.53 — both close enough to centre to cross the nearest
   rule-of-thirds line, so the arrow told the user to move the camera
   **the wrong way**. The earlier browser test missed it because its
   subject was far enough off-centre to survive the pull. Fixed by
   keeping only the top few percent of saliency mass and taking a
   saliency-weighted centroid: error against four known-position
   subjects dropped to ≤1px per axis, and the directions came back
   correct.

Because the resulting film-stock margins are genuinely small, the
response also carries `confident`; when the top two are within
`CONFIDENT_MARGIN` the sentence says *"no strong preference here"* and
the strip stops auto-applying, rather than dressing a coin flip up as a
recommendation. The same principle drove exposing both `move_subject_*`
and its inverse `pan_camera_*` explicitly: instructions read the latter,
because rendering the former as an arrow is correct arithmetic turned
into confidently backwards advice.

Verified live in a browser against the real 3-node cluster. The sandbox
has no webcam, so the camera was replaced with a canvas stream whose
composition was known in advance — subject drawn hard left at x≈0.12,
whose only correct instruction is "pan left". The overlay produced
exactly that (plus TILT DOWN, also correct for the drawn centroid), and
the subject box landed at centre (0.125, 0.471) against a drawn truth of
(0.121, 0.469). The crop rectangle measured exactly 3:4 at 1.52×,
matching its own label. Across the whole session all three nodes stayed
at 109 events — the live preview traffic wrote nothing, as designed.
Killing all three nodes cleared the overlay to *"GUIDANCE OFFLINE ·
SHUTTER UNAFFECTED"* rather than leaving stale boxes on screen, the
shutter still queued a frame to the outbox with the cluster down, and
guidance resumed on its own when the nodes came back.

**Phone testing (HTTPS).** `getUserMedia` needs a secure context and
only `localhost` is exempt, so over plain HTTP the app loads on a phone
and silently has no camera at all. `client-2/vite.config.ts` now serves
dev HTTPS from a gitignored `certs/` pair (LAN IPs in `subjectAltName`,
since CN hasn't been honoured for host matching in years) and proxies
`/n1,/n2,/n3` to the three nodes — which also solves the two problems
HTTPS creates on its own: an HTTPS page can't fetch `http://` (mixed
content), and `127.0.0.1` means *the phone* when the page runs on one.
The guest app follows `VITE_NODE_URLS`; the operator console is
untouched because it reads `CLUSTER` directly. Real-camera behaviour is
still unverified by this session — the sandbox has no webcam and the
in-app browser won't accept a self-signed cert.

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
