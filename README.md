# SwarmLens — three-node gossip backend

Three identical FastAPI nodes, each owning its own SQLite file. No shared
database. State converges by gossip, not by a central authority.

## Run

    docker compose up --build

Nodes land on http://localhost:8001, :8002, :8003

Local (no Docker), three terminals:

    NODE_ID=node1 DB_PATH=./node1.db SELF_URL=http://127.0.0.1:8001 PEERS=http://127.0.0.1:8002,http://127.0.0.1:8003 uvicorn main:app --port 8001
    NODE_ID=node2 DB_PATH=./node2.db SELF_URL=http://127.0.0.1:8002 PEERS=http://127.0.0.1:8001,http://127.0.0.1:8003 uvicorn main:app --port 8002
    NODE_ID=node3 DB_PATH=./node3.db SELF_URL=http://127.0.0.1:8003 PEERS=http://127.0.0.1:8001,http://127.0.0.1:8002 uvicorn main:app --port 8003

`SELF_URL` must be the address the *other* nodes reach this one at (i.e.
it appears in their `PEERS` list) — it's how every node's own identity on
the consistent-hashing ring matches how peers already refer to it.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/photos` | upload (guest_id, zone, composition_score, optional vclock) |
| GET  | `/photos` | gallery as this node currently sees it; each photo carries `vclock` + `concurrent_with` |
| POST | `/likes`  | like a photo (guest_id, photo_id, optional vclock) |
| GET  | `/zones`  | emergent aesthetic map, ranked; each zone's score comes from its owning node on the hash ring (`stale: true` if that node is unreachable) |
| GET  | `/zones/local` | this node's own replica of every zone, no ownership filtering — what peers proxy to |
| GET  | `/zones/ring` | debug: current ring membership and the zone → owner mapping |
| GET  | `/zones/quorum?R=&W=` | quorum read: union raw events from R of N random nodes and recompute; W is echoed back for the CAP narrative only |
| GET  | `/zones/events` | this node's raw photo/like/aesthetic_score events, undigested — what quorum reads fetch from each sampled peer |
| POST | `/analyze` | photo_id + image_base64 -> {ar_guide, suggested_filter, aesthetic_score}; writes an aesthetic_score event |
| GET  | `/health` | event count, version vector, gossip stats |
| POST | `/gossip/sync` | peer-to-peer: exchange digest, return missing events |
| POST | `/gossip/push` | peer-to-peer: deliver events the peer lacks |
| POST | `/chaos/partition/{i}` | stop gossiping with PEERS[i] |
| POST | `/chaos/heal` | resume all gossip |
| POST | `/raft/request_vote` | peer-to-peer: cast a vote for a candidate |
| POST | `/raft/heartbeat` | peer-to-peer: leader keepalive |
| GET  | `/raft/status` | this node's Raft role, term, current leader |
| POST | `/recap/trigger` | fire the one-time recap; no-ops unless this node is leader |
| POST | `/cloud_sync/trigger` | force a cloud archive sync tick now; no-ops unless leader + SUPABASE_URL configured |
| GET  | `/cloud_sync/status` | enabled/role/last sync count/last error |
| GET  | `/dashboard` | static chaos + metrics dashboard (dashboard.html), served identically by every node |

## Verified demo

    # write to node1, read from node3 a second later
    curl -X POST localhost:8001/photos -H 'content-type: application/json' \
      -d '{"guest_id":"g1","zone":"flower_arch","composition_score":93}'
    curl localhost:8003/photos

    # partition, like from both sides, watch counts diverge
    curl -X POST localhost:8003/chaos/partition/0
    curl -X POST localhost:8003/chaos/partition/1
    curl -X POST localhost:8001/likes -H 'content-type: application/json' \
      -d '{"guest_id":"g2","photo_id":"ph_xxx"}'
    curl -X POST localhost:8003/likes -H 'content-type: application/json' \
      -d '{"guest_id":"g5","photo_id":"ph_xxx"}'
    curl localhost:8001/photos ; curl localhost:8003/photos   # different

    # heal, watch them converge with nothing lost
    curl -X POST localhost:8001/chaos/heal
    curl -X POST localhost:8003/chaos/heal

Observed run: node1=3, node3=2 while split; all three = 5 after healing.
A guest who liked twice was counted once.

## Raft leader election

Election-only Raft in `raft.py` (no log replication -- the event log +
gossip already handle that). Run `python test_raft.py` for an automated
demo: starts three nodes, confirms a single leader, `kill -9`s it,
confirms re-election within ~1s, and confirms `recap_sent` fires exactly
once cluster-wide even across the crash.

## Consistent hashing (zone ownership)

Each venue zone is owned by exactly one node, picked from a consistent
hash ring (`hashing.py`, 100 virtual nodes per member) over the currently
alive cluster (self + `gossip.alive_peers()`). `GET /zones` proxies each
zone to its owner; if the owner doesn't answer it falls back to this
node's own replica with `stale: true` rather than failing. `python
test_hashing.py` is a pure unit test confirming the ring's remap
property: adding a 4th node to a 3-node ring remaps roughly 1/4 of zones,
not all of them.

## Quorum reads (the CAP knob)

`GET /zones/quorum?R=2&W=2` samples `R` of `N` nodes at random each call
(`N = len(PEERS) + 1`, fixed), unions their raw photo/like events deduped
on `(origin, seq)`, and recomputes zone scores from the merge -- more
honest than trusting one node's already-aggregated counts. `W` is
accepted and echoed back for the CAP-tradeoff narrative only; this
system's writes are always local + eventually gossiped, so there's no
synchronous write-quorum for `W` to gate.

Demo the tradeoff live:

    # fully isolate node3 (both directions, from both other nodes)
    curl -X POST localhost:8001/chaos/partition/1
    curl -X POST localhost:8002/chaos/partition/1
    curl -X POST localhost:8003/chaos/partition/0
    curl -X POST localhost:8003/chaos/partition/1

    # write after the partition -- reaches node1 + node2, never node3
    curl -X POST localhost:8001/photos -H 'content-type: application/json' \
      -d '{"guest_id":"g1","zone":"new_zone","composition_score":93}'

    curl 'localhost:8001/zones/quorum?R=1'   # sometimes lands on node3, undercounts
    curl 'localhost:8001/zones/quorum?R=2'   # always fresh -- majority excludes node3

    curl -X POST localhost:8001/chaos/heal
    curl -X POST localhost:8002/chaos/heal
    curl -X POST localhost:8003/chaos/heal   # R=1 becomes reliably fresh again

`python test_quorum.py` runs this exact scenario automatically: R=1 over
30 tries came back fresh 21 and stale 9 times, R=2 was fresh 20/20, and
R=1 was fresh 10/10 again after healing.

## AI analysis engine

`POST /analyze` (`ai_engine.py`) runs three pretrained, non-training
models on a photo and returns the results, no training or fine-tuning
anywhere:

1. **AR reframe guide** -- OpenCV saliency
   (`cv2.saliency.StaticSaliencySpectralResidual`) finds the subject,
   compared against the nearest rule-of-thirds intersection.
2. **Film-stock suggestion** -- CLIP ViT-B/32 (ONNX, int8-quantized),
   cosine similarity against a fixed set of text prompts, one per film
   stock (see `FILM_STOCKS` in `ai_engine.py`).
3. **Aesthetic score (0-10)** -- reuses the same CLIP embedding from
   step 2 with LAION's `aesthetic-predictor` v1 linear head (ViT-B/32
   variant). See `ai_engine.py`'s module docstring for exact model
   sources/versions.

Models download on first use into `./models/` (~154MB, gitignored,
cached after that) -- pre-warm before a demo with:

    python -c "import ai_engine; ai_engine.warmup()"

The resulting `aesthetic_score` is written to the event log exactly like
a photo or like event (`store.append_local`) -- it gossips, merges, and
feeds `/zones`/`/zones/quorum`'s new `avg_aesthetic` field the same way
everything else already does, no second replication path.

    curl -X POST localhost:8001/photos -H 'content-type: application/json' \
      -d '{"guest_id":"g1","zone":"flower_arch","composition_score":93}'
    # (grab the returned photo_id, then:)
    curl -X POST localhost:8001/analyze -H 'content-type: application/json' \
      -d '{"photo_id":"ph_xxx","image_base64":"<base64 png/jpeg>"}'
    curl localhost:8001/zones   # avg_aesthetic now populated for that zone

`python test_ai_engine.py` runs this end to end against 3 live nodes:
confirms the AR guide direction is sane for an intentionally off-center
subject, confirms suggested_filter differs between a sunset-gradient and
a flat-gray image, and confirms the aesthetic_score event count matches
across all three nodes' `.db` files after a gossip round.

## Vector clocks (concurrent events)

`events.vclock` (JSON, envelope metadata like origin/seq) lets a guest
device attach its own `{device_id: counter}` causal clock to a photo or
like. `GET /photos` uses it to compute `concurrent_with` per photo:
other photo_ids whose clock neither dominates nor is dominated by this
one, so the gallery can render them side by side instead of implying a
false total order from network arrival timing. A photo posted with no
vclock is never flagged concurrent with anything -- true of everything
posted by curl, but not of photos from the guest client in `client/`
(Phase 5's other half), which attaches a real one to every capture.

    curl -X POST localhost:8001/photos -H 'content-type: application/json' \
      -d '{"guest_id":"a","zone":"bar","vclock":{"deviceA":1}}'
    curl -X POST localhost:8001/photos -H 'content-type: application/json' \
      -d '{"guest_id":"b","zone":"bar","vclock":{"deviceB":1}}'
    curl localhost:8001/photos   # each lists the other in concurrent_with

`python test_vclock.py` simulates a couple of devices this way against 3
live nodes and confirms the concurrency relationships are correct and
survive gossip replication unchanged.

## Cloud archive sync

`cloud_sync.py` pushes the event log to a Supabase/PostgREST-style
endpoint, one-way and append-only, on a leader-gated background loop
(`CLOUD_SYNC_INTERVAL`, default 15s). Disabled entirely unless
`SUPABASE_URL` is set -- zero effect on a node that doesn't configure it.
It's config-driven, not hardcoded to a real project (none exists for
this repo yet):

    SUPABASE_URL=https://xxxx.supabase.co
    SUPABASE_KEY=<service-role or scoped key>
    SUPABASE_EVENTS_TABLE=events   # optional, this is the default

The destination table needs columns matching the event shape (`origin
text, seq int8, kind text, payload jsonb, created_at float8, vclock
jsonb`) and a `UNIQUE(origin, seq)` constraint -- that's what makes
repeated/overlapping pushes safe to dedup via `Prefer:
resolution=ignore-duplicates`. See `cloud_sync.py`'s module docstring for
the full reasoning, including why a *local, non-replicated* sync
checkpoint is still correct even across a Raft leadership change.

    curl -X POST localhost:8001/cloud_sync/trigger   # force a tick now
    curl localhost:8001/cloud_sync/status

`python test_cloud_sync.py` stands up a tiny in-process fake-cloud HTTP
server (no real Supabase project needed) and confirms: the cluster keeps
working while the fake cloud isn't listening ("no internet"); a trigger
against it fails softly instead of 500ing; the full backlog syncs in one
push once it starts listening ("reconnect"); re-triggering with nothing
new is a true no-op with no duplicates; a new event afterward syncs
incrementally; and only the Raft leader ever pushes.

## Chaos + metrics dashboard

    curl -X POST localhost:8001/photos ...   # cluster running
    # open in a browser:
    http://localhost:8001/dashboard

Self-contained static HTML/CSS/vanilla JS (`dashboard.html`), served
identically by every node -- open it on any one of the three, it polls
`GET /health` on all three via `fetch` (CORS is already wide open) every
1s. Shows role/term/leader/event count/gossip stats/cloud-sync status per
node, and buttons for `/chaos/partition/{i}` and `/chaos/heal`, plus an
"Isolate this node" convenience button that fires all 4 directional
partition calls a *real* bidirectional isolation needs in one click (a
single-direction partition alone self-heals within a round or two --
see CLAUDE.md).

This is the lighter alternative to a full Prometheus/Grafana setup --
see the Phase 7 writeup in ROADMAP.md for why, and for a real bug this
caught by actually clicking through the UI in a browser rather than only
curling the endpoints it drives. No real `kill -9` button: see
ROADMAP.md's Phase 7 writeup for why that's a genuinely different risk
(an HTTP endpoint that kills an OS process, on a backend with no auth at
all) rather than scope creep.

## Load test + numbers

    python load_test.py --quick   # fast, reduced-scale, validates the script
    python load_test.py           # full spec'd numbers (200 uploads, 500
                                   # likes, 20 election trials) -- a few
                                   # minutes end to end

Plain `asyncio` + `httpx` (no locust), spins up and tears down its own
real clusters, never mocks. Measures upload/like latency percentiles,
convergence time, node-kill recovery time, leader re-election latency
(20 trials), throughput at N=1,2,3,4, and gossip bandwidth per round.
Outputs `results.json` + `summary.csv` + 6 PNGs into
`load_test_results/` (gitignored -- results from one run on one
machine, not committed source).

**Numbers from a full run on this machine** (one run -- see
`load_test_results/summary.csv` after your own run for current numbers):

| Metric | Value |
|---|---|
| Photo upload latency (200 concurrent) | p50 1.39s / p95 1.59s / p99 1.60s |
| Like latency (500 concurrent) | p50 1.53s / p95 2.54s / p99 2.71s |
| Convergence time after the burst | 1.89s |
| Recovery (kill -> reclaim -> done) | 13.25s |
| Leader re-election (20 trials) | min 0.19s / mean 0.29s / max 0.85s |
| Throughput, N=1 / 2 / 3 / 4 | 81 / 132 / 164 / 220 uploads/sec |
| Gossip `/sync` payload | cold (full backlog) 112KB / warm (steady state) 509B |

The latency numbers under full concurrent load are notably higher than
under light load, and that's the honest cost of a real bug this script
found and fixed: `store.py`'s sequence-number assignment wasn't atomic
across concurrent writers (see ROADMAP.md's Phase 8 writeup and
CLAUDE.md's Gotchas section for the full story). The fix correctly
serializes every write through one SQLite connection per node, so a
genuine 200-way concurrent burst queues -- a real architectural ceiling,
not a bug. Throughput scaling with N (81 -> 220 uploads/sec) is the
direct upside of the same fact: each additional node is another fully
independent writer.

## Guest client (client/)

Phase 5's other half: a React + TypeScript PWA with an offline Dexie.js
outbox and a `localStorage` vector clock, so guests can capture photos
with no connectivity and have them sync -- in original causal order --
once a node is reachable again.

    cd client
    npm install
    npm run dev

Defaults to talking to `localhost:8001,8002,8003`; override with
`VITE_NODE_URLS` if your cluster runs elsewhere. See
[`client/README.md`](client/README.md) for the full stack breakdown and
how it fits together with the backend -- it's a functional reference
implementation proving the offline+vclock mechanism, not the polished
branded guest UI.

## Branded guest app + operator console (client-2)

The actual branded frontend -- two separate apps (guest app, operator
console) sharing one design system -- wired to this backend in full:
real camera capture, real zone picker, real offline outbox, real
gallery/likes/heatmap, and an operator console with real Raft/gossip
state, a real event tape, and real chaos actions.

    npm --prefix client-2 run dev -- --host

Defaults to `:8080` (Vite auto-increments if that's taken -- check the
terminal output). Needs `client-2/.env` (gitignored; see
`client-2/.env.example`) with `CONSOLE_PASSWORD` and `OPERATOR_TOKEN` --
the latter also needs setting as an env var on the backend nodes above
to actually be enforced (`/chaos/*` fails open without it). See the
"Branded guest app + operator console" writeup in
[ROADMAP.md](ROADMAP.md) for what was built and the real bugs found
wiring it up, and CLAUDE.md's Files section for the file-by-file
breakdown.
