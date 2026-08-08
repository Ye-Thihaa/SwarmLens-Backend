# SwarmLens backend

Three identical FastAPI nodes (`main.py`), each with its own SQLite file —
no shared database, no central authority. State converges by gossip.
Full design and phase-by-phase plan: [ROADMAP.md](ROADMAP.md). Endpoint
list and manual demo commands: [README.md](README.md).

## Files

- `main.py` — FastAPI app + all HTTP endpoints. Same file runs as every
  node; behavior is set entirely by env vars (`NODE_ID`, `DB_PATH`, `PEERS`,
  and now optionally `OPERATOR_TOKEN` — see below). `POST /photos` accepts
  an optional `image_base64`; gossip replicates it as part of the ordinary
  `photo` event payload (no second storage path), and `GET
  /photos/{photo_id}/image` serves the decoded bytes back out — kept as a
  separate endpoint so `GET /photos` (polled every few seconds by both
  guest clients) doesn't ship every photo's full image on every call;
  `store.photos()` strips `image_base64` back out of the list response for
  the same reason, and adds `taken_at` (already on the event as
  `created_at`, just not exposed until a client needed it for display).
  `POST /analyze/preview` is the live-viewfinder sibling of `/analyze`:
  same models, same `to_thread` offload, but it appends **no** events —
  a camera calls it every 1.5s against a frame that has no `photo_id` and
  was never shot, so persisting those would flood the log, gossip every
  one to all three nodes, and drag `/zones`' `avg_aesthetic` toward
  frames no guest kept. It is bounded by a `PREVIEW_CONCURRENCY`
  semaphore and sheds excess with 503 rather than queueing (guidance for
  a frame the guest already panned away from is worse than none), and
  caps the request body — measured at ~37ms/frame with `/health` staying
  under 10ms alongside it, so the asyncio loop and raft's 50ms heartbeat
  are unaffected.
  `/chaos/partition/{i}` and `/chaos/heal` are gated by
  `require_operator_token` (checks `X-Operator-Token` against
  `OPERATOR_TOKEN` with `secrets.compare_digest`) — fails open when
  `OPERATOR_TOKEN` is unset, so `test_quorum.py`, `load_test.py`, and
  `dashboard.html`, none of which set it, keep working unauthenticated.
  `client-2`'s operator console is the one real caller that sets it (see
  that entry below).
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
  this — see its entry below for why that's still correct). `append_local`
  holds `self._write_lock` (an `asyncio.Lock`) around assigning the next
  seq + inserting — see the Gotchas section for the real concurrency bug
  this fixes. `photo_image_b64(photo_id)` is the read side of the
  image-sharing feature above — pulls straight from the same event row,
  no second table.
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
  film-stock suggestion, and a CLIP-embedding aesthetic score. Two entry
  points that differ in *persistence, not computation*: `analyze()` is the
  post-capture path whose `aesthetic_score` is written to the event log,
  and `preview()` is the live-viewfinder path (`POST /analyze/preview`)
  that computes strictly more — a subject box, a crop rectangle
  (`compute_reframe`), a dominant-colour read (`describe_scene`) and a
  templated recommendation sentence (`compose_reason`) — and writes
  nothing at all. `compute_ar_guide` returns `move_subject_*` *and* its
  inverse `pan_camera_*` explicitly, because a UI that renders the wrong
  one shows a confidently backwards arrow; it also reports `strength`
  bucketed off the real dx/dy magnitude so copy like "a little" tracks
  the measurement instead of being a fixed adverb. `FILM_STOCKS`' keys
  are a contract with `client-2/src/guest/data.ts` (rename one side alone
  and the recommendation silently stops matching anything), and each
  stock carries several short prompts that get ensembled — see the
  Gotchas entry on CLIP class priors for why one long prompt each did not
  work. Wired up as `POST /analyze` in `main.py`, which runs it on a thread (it's
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
- `dashboard.html` — static chaos + metrics dashboard, served at `GET
  /dashboard` by every node (`main.py`, `FileResponse`). No build step,
  no external JS/CSS — polls `GET /health` on all 3 nodes every 1s and
  drives `/chaos/partition/{i}` and `/chaos/heal`. This is the lighter
  alternative from Phase 7's "Prometheus/Grafana vs. lighter" fork — see
  ROADMAP.md's Phase 7 writeup for why, and for a real bug (every button
  was broken) this caught by actually clicking through it in a browser.
- `load_test.py` — plain `asyncio` + `httpx` load test (no locust), spins
  up and tears down its own real clusters (1/2/3/4 nodes as needed),
  never mocks. Measures upload/like latency percentiles, convergence
  time, node-kill recovery time, leader re-election latency (20 trials),
  throughput vs. cluster size, and gossip bandwidth. `--quick` runs a
  fast reduced-scale version for validating the script itself. Outputs
  `results.json`/`summary.csv`/PNGs into `load_test_results/`
  (gitignored). Found and fixed a real concurrency bug in `store.py`
  (see the Gotchas section) — see the full writeup and numbers from a
  real run in ROADMAP.md's Phase 8 section.
- `client/` — Phase 5's guest client: React 19 + TypeScript PWA (Vite),
  Dexie.js offline `outbox`, `localStorage` vector clock, a service
  worker with a real Background Sync handler (`vite-plugin-pwa`,
  `injectManifest` strategy — needed for a custom `sync` event handler,
  which the default `generateSW` strategy doesn't support). A functional
  reference implementation proving the offline+causal-ordering mechanism,
  not the polished branded guest UI described elsewhere in the project's
  task list. Also has a working like button (`POST /likes`) and renders
  other guests' photos for real via `GET /photos/{id}/image` — falls back
  to a metadata-only placeholder, never a broken-image icon, for older
  captures with no `image_base64` attached. See `client/README.md` for
  the full stack breakdown and rationale, and the Phase 5 writeup in
  ROADMAP.md for what was verified live in a browser against the real
  backend.
- `client-2/` — the actual branded guest app + operator console (two
  separate apps sharing one design system), originally a Lovable-
  generated TanStack Start scaffold with zero backend calls anywhere
  (static bundled images standing in for the camera, fake
  `killLeader()`/`partition()` client state) — now fully wired to the
  real cluster, closing the gap `client/`'s entry above used to describe
  as unattached. See the "Branded guest app + operator console" writeup
  in ROADMAP.md for the full list of what got wired and the real bugs
  found doing it. Highlights:
  - `src/lib/api.ts` — the node-picking/fetch client (same "race
    `/health`, use whichever answers first" pattern as `client/src/nodes.ts`).
  - `src/lib/outbox.ts` — offline photo/like queue, localStorage-backed
    (this app has no Dexie yet, unlike `client/`) — `useOutbox()` is a
    `useSyncExternalStore` hook; see the Gotchas entry on caching its
    snapshot.
  - `POST /analyze` fires automatically after each photo syncs (see
    `outbox.ts`) — the first time either client has actually exercised
    `ai_engine.py`'s aesthetic pipeline instead of leaving it dark.
  - `src/routes/capture.tsx` also runs a live **AI compose** overlay
    before the shutter: a 1.5s loop posts a 224px frame to
    `/analyze/preview` and draws the subject box, the crop rectangle with
    its zoom factor, a camera-move instruction, and the recommendation
    sentence. `grabPreviewFrame()` center-crops the video to the selected
    ratio before sending — a correctness requirement, not bandwidth
    thrift, since the returned rects are normalized to whatever frame was
    sent and get painted straight onto the displayed video box. Every
    element is gated on the engine actually having found something
    (`subject_found`, `worth_it`, `confident`), and the whole overlay
    goes silent — not stale — when no node answers, since capture and the
    outbox are local and a partition should cost a guest advice, never a
    photo.
  - `src/routes/console.tsx` — the operator console: real Raft
    term/role and gossip/partition state polled from all 3 nodes, an
    event tape built from *observed state diffs* (term changes, peer
    reachability flips) rather than scripted log lines, and chaos
    actions relabeled honestly — "Isolate the leader", not "Kill", since
    partitioning doesn't force a re-election (`/chaos/partition` only
    stops gossip; raft's heartbeats bypass it entirely, same gotcha as
    below).
  - `src/lib/consoleAuth.ts` — real password gate on `/console`: a
    TanStack server function verifies the password server-side and
    issues an HttpOnly session cookie; the route's `loader` checks it on
    every request including the first SSR render, so an unauthenticated
    visitor's HTML never contains the console's data.
  - `src/lib/operatorGateway.ts` — chaos actions route through server
    functions (not straight from browser to FastAPI backend like the
    read-only calls in `api.ts` still do), so `main.py`'s
    `OPERATOR_TOKEN` is read from `process.env` server-side and never
    has to touch client JS. Also re-checks the console session itself,
    on every call — a `beforeLoad` route guard alone doesn't protect a
    server function, since it's reachable independently of the route
    that happens to call it.
  - `client-2/.env` (gitignored; `.env.example` documents the shape)
    holds `CONSOLE_PASSWORD` and `OPERATOR_TOKEN`.

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

Guest client (needs the backend already running): `cd client && npm
install && npm run dev` — Vite dev server on :5173, talks to
`localhost:8001-8003` by default (override with `VITE_NODE_URLS`). A
`.claude/launch.json` entry (`swarmlens-client`) is already set up for
`preview_start` if driving it through the Browser tool.

Branded guest app + operator console (also needs the backend running):
`npm --prefix client-2 run dev -- --host`. Defaults to :8080, but Vite
auto-increments if that's taken — check the terminal output for the
real port, especially if an old instance from a previous session is
still holding it (see the Gotchas entry on this). `client-2/.env`
(gitignored, already present locally) holds `CONSOLE_PASSWORD` and
`OPERATOR_TOKEN`; to actually enforce the latter, the backend nodes
above need the matching `OPERATOR_TOKEN` env var set too (optional —
`/chaos/*` fails open without it). A `.claude/launch.json` entry
(`swarmlens-client-2`) is set up for `preview_start`.

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
- `python load_test.py [--quick]` — not a pass/fail test, a measurement
  script. Spins up and tears down its own real clusters, fires real
  concurrent traffic, reports p50/p95/p99 latency, convergence time,
  node-kill recovery time, 20-trial election latency, throughput at
  N=1..4, and gossip bandwidth. `--quick` uses reduced counts to
  validate the script fast; full mode (the spec'd 200 uploads/500
  likes/20 trials) takes a few minutes. See ROADMAP.md's Phase 8 section
  for numbers from a real run and two real bugs this caught (one in
  `store.py`, one in the load test's own recovery-measurement logic).
- No test file yet for gossip/worker phases — those were verified
  manually per the curl sequences in README.md. Same for the live
  `/zones` ownership/proxy/stale-fallback behavior — verified manually
  (see the Phase 3 writeup in ROADMAP.md for what was checked).

## Current status

All of Phases 0-8 are done (gossip replication, worker leases, Raft
election, consistent-hash zone ownership, quorum reads, vector clocks —
backend *and* a working guest client in `client/` — cloud archive sync,
chaos/metrics dashboard, load test), plus the AI analysis engine
(`ai_engine.py`, `POST /analyze`) that sits outside the phase numbering.
See ROADMAP.md for the full phase-by-phase writeup, including two real
bugs Phase 8's load test found (one in `store.py`, described in the
Gotchas section below), the actual measured numbers, and what was
verified live in a browser for the guest client.

Also done, and also outside the phase numbering: the *actual* branded
guest app + operator console now live in this working directory
(`client-2/`, added mid-project) and are fully wired to the real
backend — real camera capture, real gallery/likes/heatmap, a real
operator console (Raft/gossip state, chaos actions, quorum reads), and
a real password gate on `/console` plus an optional backend
`OPERATOR_TOKEN`. See the "Branded guest app + operator console"
writeup in ROADMAP.md for what was built, the real bugs found wiring
it up, and what's still a known simplification (a single "room" since
this backend has no multi-event concept, and a localStorage outbox
instead of `client/`'s Dexie/service-worker one).

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
- **`store.append_local`'s "read the seq counter, +1, write it back" is
  a race unless it's a single critical section.** It spans an `await`
  (`SELECT` ... compute ... `UPDATE`), so two concurrent calls can both
  read the same counter value before either writes back, both assign the
  same `seq`, and collide on `events`' `(origin, seq)` UNIQUE constraint.
  Every test/demo in this repo only ever awaited one write at a time
  until `load_test.py` fired genuinely concurrent requests — 200 uploads
  at one node threw `sqlite3.IntegrityError` reliably. Fixed with
  `Store._write_lock` (`asyncio.Lock`) around the whole "assign next seq
  + insert" section in `append_local`. If you add another code path that
  writes local events, it must go through `append_local`, not reimplement
  its own seq assignment.
- **A system-wide loopback-intercepting proxy breaks Raft's 50ms
  heartbeat/150-300ms election timeout outright, and `trust_env=False`
  can't fix it.** Confirmed on a dev machine with Windows' system proxy
  enabled (`HKCU:\...\Internet Settings`, `ProxyEnable=1`) pointing at
  `127.0.0.1:<port>` — even a bare `GET /health` on a single node measured
  ~500-900ms median round-trip on loopback, vs. the sub-millisecond
  latency Raft's timers assume. `trust_env=False` (see the note above)
  only stops httpx from *choosing* to route through a configured proxy;
  it can't defeat something intercepting sockets below the application
  layer. Symptom: `test_raft.py`/`test_quorum.py`/`test_cloud_sync.py`
  see leadership flip continuously, not just at boot, and
  `test_cloud_sync.py`'s leader-targeted calls land on a stale leader.
  No code fix applied for this — the two real options are disabling the
  intercepting proxy for the test run, or raising `raft.py`'s timing
  constants (which would also slow down every number in ROADMAP.md's
  Phase 8 section, so don't do that silently). If you hit flaky
  leader-dependent tests, check for this before assuming a code
  regression.
- **A load-test measurement that detects state via one "observer" node
  is measuring gossip lag, not the thing it's trying to measure.** The
  first version of `load_test.py`'s recovery measurement polled a single
  node to detect when a job got claimed, then killed the claimant. A
  node's own writes are visible to *itself* instantly; to an observer,
  only after the next gossip round. That gap was large enough that the
  claimant sometimes finished its whole 5-second fake-work window before
  the kill signal landed — silently measuring "did the worker finish
  before I noticed and killed it" (suspiciously exactly 5.0s) instead of
  a genuine kill-mid-work scenario. Fixed by polling all nodes directly
  and in parallel, catching the claim via the claimant's own zero-lag
  self-report. If you write another measurement that needs to catch a
  node in a specific state fast, poll every node, not one.
- **`pydantic==2.10.4` (the old pin) has no Python 3.14 Windows wheel,
  and every internal `httpx` client defaults to trusting a system
  proxy.** Two separate environment gotchas that both surfaced only once
  someone actually tried running this on a real Windows machine (not
  this dev sandbox): (1) `pydantic-core` only ships a `cp314-win_amd64`
  wheel from 2.40.0 onward -- older pins force pip to compile from
  source via Rust + the MSVC linker, which fails without Visual Studio
  Build Tools installed. Fixed in `requirements.txt` with an explicit
  `pydantic-core>=2.40` floor (pydantic's own version number doesn't
  track pydantic-core's, so constraining only `pydantic` isn't enough).
  (2) `httpx`'s default `trust_env=True` means any client honors
  `HTTP_PROXY`/system proxy settings -- on a machine with one configured
  that doesn't exempt loopback traffic, every 127.0.0.1-only call in
  this codebase (gossip, raft, the `/zones` proxy calls, every test
  script, `load_test.py`) intermittently 502s / connection-resets /
  returns empty bodies. Every client that *only* ever talks to
  127.0.0.1 now hardcodes `trust_env=False`. The two exceptions that
  must NOT get this: `ai_engine.py`'s model downloader and the `/analyze`
  endpoint's photo-`url` fetch (both may need to reach the real
  internet), and `cloud_sync.py`'s client (must honor a real proxy to
  reach a real Supabase in production) -- its trust setting is
  controlled by `CLOUD_SYNC_TRUST_ENV` instead, which `test_cloud_sync.py`
  flips to `false` only because its "cloud" is a local fake server.
- **A test script that spins up its own nodes will silently attach to
  already-running ones on the same ports instead of erroring.**
  `test_vclock.py` (and likely the other `test_*.py` scripts) hardcode
  ports 8001-8003 — running one while the demo cluster is already up on
  those same ports doesn't fail to bind, it just posts the test's
  synthetic events (guest `g1`, devices `deviceA`/`deviceB`) straight
  into the live demo's real databases, polluting them. Confirmed live:
  a test run bumped the live cluster's event count and left a fake
  guest sitting in the real gallery. Don't run these test scripts
  against a cluster you care about the data in — check `netstat`/`lsof`
  for 8001-8003 first, or only run them when nothing else is up.
- **Zero-shot CLIP with one hand-written prompt per class has a class
  prior that can swamp the actual signal.** The film-stock suggester
  originally used one long prompt per stock. Measured against 18 real
  reference frames — a night motorcycle, a dappled-sun truck, a sunflower
  field, and near-black end cards — **one stock won all 18**, because a
  long prompt full of generic photographic words picks up a constant
  offset (`gold_200` sat at a mean +0.034 while the rest were near zero)
  larger than the per-image variation (0.02–0.056). Two fixes together:
  mean-center the text embeddings to remove the component every prompt
  shares, and ensemble several *short* prompts per class (CLIP's own
  zero-shot recipe). After both, the same 18 frames split across 4
  different stocks. If you add a stock, keep its prompts short and
  comparable in specificity, and re-measure the winner distribution
  rather than eyeballing one image. Because the resulting margins are
  genuinely small, `preview()` also reports `confident`
  (`CONFIDENT_MARGIN`) so a coin-flip is never rendered in the same voice
  as a clear win.
- **Excluding "blown highlights" with `V > 250` throws away every pixel
  of a fully saturated image.** `describe_scene`'s first version masked
  out both very dark and very bright pixels before its hue histogram.
  A vivid orange is `V=255` with plenty of hue, so a bright sunset came
  back as *"dim, near-colorless light"* with mean S and V both exactly
  0.0 — the mask had emptied the array. The correct test for "this pixel
  has no usable hue" is the saturation gate, not brightness; only the
  dark floor is a legitimate exclusion.
- **Locating a subject in a saliency map: two obvious approaches are
  both wrong, and the second is wrong in a way that inverts the
  guidance.** Worth reading before "improving" `_saliency_subject`,
  because both were tried and measured.
  (1) *Whole-image statistics over every above-Otsu pixel* (min/max, or
  a 6th/94th percentile) collapse to ~90% of the frame on any real
  photograph — foliage, gravel and fabric speckle saliency everywhere —
  which silently makes the reframe a permanent no-op, since a crop that
  size is never tighter than what's already framed.
  (2) *The strongest connected component* fixes that and breaks
  differently: spectral residual responds to **edges, not filled
  regions**, so one object answers as several fragments (a uniform
  square splits into separate left-edge and right-edge blobs) and the
  winning fragment's centroid sits off to one side. Worse, the dimly-lit
  background that survives thresholding drags the centroid toward the
  frame centre — measured on synthetic subjects at x=0.22 and x=0.72,
  the reported positions were 0.40 and 0.53, both close enough to centre
  to cross the nearest rule-of-thirds line and **tell the user to move
  the camera the wrong way**. Correct arithmetic, backwards advice,
  which is the exact failure this feature is supposed to avoid.
  The fix that holds: keep only the top `SUBJECT_TOP_PCT` of the blurred
  map's saliency mass (drops the background doing the centre-pulling),
  weight by height *above* that threshold, and take the weighted
  centroid — an object's several edge responses then average back onto
  the object. Extent comes from the weighted standard deviation, and
  `found` is false when that spread exceeds `SUBJECT_DIFFUSE_MAX`.
  Centroid error against four synthetic subjects at known positions is
  ≤1px per axis; if you change any of this, re-run that check rather
  than eyeballing one photo, since the failure mode is a *plausible*
  box in the wrong place.
- **`test_ai_engine.py` (and its siblings) `os.remove()` `./node1.db`
  through `./node3.db` on cleanup — the same files the demo cluster
  uses.** This is the sharper edge of the port-collision gotcha below: it
  isn't only that a test run pollutes live data, it's that the run
  *deletes those databases when it finishes*. And because the test also
  attaches to whatever DBs already exist, running it against a populated
  demo cluster fails in a confusing way first (`expected 3
  aesthetic_score events` counting 109 pre-existing ones) before wiping
  them. Back the `.db` files up, or only run these with nothing else set
  up on 8001-8003.
- **`useSyncExternalStore`'s snapshot function must return a stable
  reference when nothing changed, or it loops forever.**
  `client-2/src/lib/outbox.ts`'s first version re-parsed the
  localStorage JSON blob on every call to `getSnapshot` — a *new* array
  every time, even when the underlying data hadn't changed, which React
  reads as "changed" on every render and re-renders forever ("Maximum
  update depth exceeded"). Fixed by caching against the raw string and
  only re-parsing when it actually differs. If you add another
  `useSyncExternalStore`-backed store anywhere, the snapshot function
  needs the same caching, not just "return the current value."
- **In TanStack Start, `src/start.ts` is isomorphic (bundled for both
  client and server); `src/server.ts` is not.** Importing a Node
  built-in (`node:fs`, `node:path`) at the top of `start.ts` breaks
  client-side hydration with a cryptic "Module has been externalized
  for browser compatibility" error, because Vite has to stub those
  modules out for the browser bundle. `client-2/src/server.ts` (the
  actual Nitro server entry, see `vite.config.ts`'s `server: { entry:
  "server" }`) is the safe place for Node-only bootstrap code like
  `client-2`'s own `.env` loader — never `start.ts`.
- **Vite's own `.env` loading treats `$` as variable-interpolation
  syntax (`dotenv-expand`), even for non-`VITE_`-prefixed keys your own
  code never asked it to touch.** `client-2/.env`'s
  `CONSOLE_PASSWORD=c0n$ol3` silently became `"c0n"` server-side —
  `$ol3` was read as a reference to an undefined env var `ol3` and
  expanded to nothing — before `client-2/src/server.ts`'s own loader
  (which does zero interpolation) ever ran. A guarded "only set if not
  already present" write kept that mangled value; the fix was making
  the custom loader unconditional so the literal file value always
  wins. If a secret contains `$`, don't trust any `.env` value you
  haven't verified end-to-end.
- **Testing the camera on a real phone needs three separate things, and
  each fails silently on its own.** `client-2`'s capture route uses
  `getUserMedia`, which requires a *secure context*; only `localhost` is
  exempt. So on a phone at `http://192.168.x.x:8080` the app loads
  perfectly and simply has no camera — the route's own error copy
  ("NEEDS HTTPS OR LOCALHOST") is the only clue. Enabling HTTPS then
  breaks the backend calls two more ways: an HTTPS page may not fetch
  `http://` URLs (mixed content), and `127.0.0.1:8001` means *the phone*
  when the page is running on a phone. `vite.config.ts` handles all
  three: a dev cert from `certs/` (gitignored; the LAN IPs must be in
  `subjectAltName` — browsers stopped honouring CN for host matching
  years ago), plus a `/n1,/n2,/n3` same-origin proxy to the nodes that
  the guest app is pointed at with `VITE_NODE_URLS`. The operator
  console is deliberately unaffected: it reads `api.ts`'s `CLUSTER`
  (absolute URLs) rather than `NODES`, because `/chaos/partition`
  indexes positionally into a specific node's own `PEERS` list. Missing
  cert files fall back to HTTP rather than failing to boot.
- **A dev server bound with `--host` on a port that's already taken
  silently moves to the next one, and the old process on the old port
  doesn't stop just because you meant to replace it.** Running
  `client-2`'s dev server twice (e.g. once from a previous session,
  once fresh) leaves two live processes — one on :8080, one on :8081 —
  both serving the app, but only the newer one has the latest code.
  Hitting the stale one looks exactly like a real bug (e.g. a correct
  password being rejected) until you check `netstat`/`lsof` for extra
  listeners and close the old terminal.

## Conventions

- Every derived read (photo list, zone scores, job status, like counts)
  is computed by replaying `events`, never stored redundantly. If you add
  a new feature, follow this pattern rather than adding a second source
  of truth.
- No new distributed-systems mechanism beyond what ROADMAP.md lists.
  Every extra protocol is extra failure-mode surface — see "What NOT to
  add" at the bottom of ROADMAP.md.
