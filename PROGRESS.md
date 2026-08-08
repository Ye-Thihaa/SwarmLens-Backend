# SwarmLens — progress report

_Last updated: 8 August 2026._

A three-node, no-central-authority photo system for live events. Three
identical FastAPI nodes, each with its own SQLite file, converging by
gossip; two guest frontends and an operator console on top. Full design
in [ROADMAP.md](ROADMAP.md), endpoint list in [README.md](README.md),
working notes and hard-won gotchas in [CLAUDE.md](CLAUDE.md).

## Status at a glance

| Area | State |
|---|---|
| Phases 0–8 (the whole planned backend) | Done, tested |
| AI analysis engine (`ai_engine.py`) | Done, tested |
| Live AI compose guidance (pre-shutter) | Done, verified on a real phone |
| Guest client `client/` (reference PWA) | Done |
| Branded guest app + operator console `client-2/` | Done, wired to the real cluster |
| Load test + measured numbers | Done |

Nothing in the roadmap is outstanding. What remains are documented
simplifications, listed at the bottom.

## The distributed-systems core

| Phase | What | Proof |
|---|---|---|
| 0 | Append-only event log, gossip anti-entropy, CRDT reads | photo reaches all nodes in one round; likes during a partition converge with none lost |
| 1 | Worker leases (8s, heartbeat-renewed, reclaimable) | `kill -9` the claimant → exactly one `job_done` cluster-wide |
| 2 | Raft leader election (hand-rolled, no library) | re-election in 0.19–0.85s; recap fires exactly once across a crash |
| 3 | Consistent-hash zone ownership | identical ring on all 3 nodes; 4th node remaps ~23% of zones |
| 4 | Quorum reads (`/zones/quorum?R=&W=`) | R=1 stale 9/30 under partition, R=2 fresh 20/20 |
| 5 | Vector clocks + `concurrent_with` | mutual concurrency for independent devices, never false concurrency |
| 6 | Cloud archive sync (leader-gated, one-way) | full backlog syncs on reconnect; no duplicates; followers no-op |
| 7 | Chaos + metrics dashboard | UI reacts within one 1s poll of every chaos action |
| 8 | Load test | see numbers below |

Every derived read — photo list, zone scores, like counts, job status —
is replayed from `events`. There is no second source of truth anywhere.

### Measured performance (full run, one machine)

| Metric | Value |
|---|---|
| Upload latency, 200 concurrent | p50 1.39s / p95 1.59s / p99 1.60s |
| Like latency, 500 concurrent | p50 1.53s / p95 2.54s / p99 2.71s |
| Convergence after the burst | 1.89s |
| Recovery (kill → reclaim → done) | 13.25s |
| Leader re-election, 20 trials | min 0.19s / mean 0.29s / max 0.85s |
| Throughput, N=1/2/3/4 | 81 / 132 / 164 / 220 uploads/sec |
| Gossip `/sync` payload | 112KB cold, 509B steady state |

The interesting result is not the latency but *why* it degrades: one
SQLite writer per node means request #200 queues behind 199 others. The
same fact is the upside — each added node is another independent writer,
so throughput scales nearly linearly with N.

## AI analysis

`ai_engine.py`, pretrained only, nothing trained or fine-tuned here:
OpenCV spectral-residual saliency, CLIP ViT-B/32 (int8 ONNX) for
film-stock matching, and LAION's aesthetic-predictor linear head reusing
the same CLIP embedding. Two entry points that differ in **persistence,
not computation**:

- `POST /analyze` — after a capture; writes an `aesthetic_score` event
  that gossips like anything else and feeds `/zones`' `avg_aesthetic`.
- `POST /analyze/preview` — live viewfinder; computes strictly more (a
  subject box, a crop rectangle with zoom, a camera-move instruction,
  ranked film stocks, a reason sentence) and **persists nothing**. A
  camera calls it every 1.5s against a frame that has no `photo_id` and
  was never shot; recording those would flood the replicated log and skew
  `avg_aesthetic` with frames no guest kept. Semaphore-bounded, sheds
  with 503 rather than queueing. ~37ms/frame with `/health` staying under
  10ms alongside it, so raft's 50ms heartbeat is untouched.

### Honesty constraints built into the output

This is guidance a person acts on, so the failure mode that matters is
*confident wrongness*, not imprecision:

- `pan_camera_*` is returned alongside its inverse `move_subject_*`, and
  instructions must read the former — rendering the wrong one is correct
  arithmetic turned into a backwards arrow.
- `strength` buckets the real dx/dy magnitude, so "a little" tracks a
  measurement rather than being a fixed adverb.
- `confident` is false when the top two film stocks fall within
  `CONFIDENT_MARGIN`; the UI then says "no strong preference here" and
  stops auto-applying.
- `subject_found` false means the overlay draws nothing at all and says
  "NO CLEAR SUBJECT", rather than boxing noise.

## Frontends

- **`client/`** — Phase 5 reference PWA: Dexie outbox, localStorage vector
  clock, real Background Sync service worker. Proves the offline +
  causal-ordering mechanism end to end.
- **`client-2/`** — the actual product. Branded guest app (real camera,
  zones, gallery, likes, heatmap, offline outbox, live AI compose overlay)
  plus an operator console (real Raft/gossip state, honest chaos actions,
  quorum demo) behind a server-side password gate, with chaos calls routed
  through server functions so `OPERATOR_TOKEN` never reaches client JS.

Dev HTTPS + a `/n1,/n2,/n3` same-origin proxy make phone testing work:
`getUserMedia` needs a secure context (only `localhost` is exempt), and
once you enable HTTPS an HTTPS page can't call `http://`, and `127.0.0.1`
means *the phone*. All three are handled in `client-2/vite.config.ts`.

## Verified on real hardware

Live on an iOS phone against the real 3-node cluster: correct arrows on
real scenes (a subject just right of the thirds line → PAN RIGHT; a
low-left subject → PAN LEFT + TILT DOWN), apt film-stock calls (RGB
lighting in a dim room → Cinestill 800T, *"lets the lights bloom the way
tungsten night film does"*; a grey desk → Tri-X 400), and captures from
the phone replicating to all three nodes (109 → 127 events, new guest
visible cluster-wide).

Also confirmed: with all three nodes killed the overlay clears to
"GUIDANCE OFFLINE · SHUTTER UNAFFECTED" rather than showing stale boxes,
the shutter still queues to the outbox, and guidance resumes unprompted
when the cluster returns.

## Bugs worth remembering

Full list with reproduction details in CLAUDE.md's Gotchas. The ones that
changed how the system is built:

1. **Raft heartbeat must be well under the election timeout**, and so must
   the per-RPC timeout — otherwise a dead peer stalls every election round
   into a permanent two-node livelock.
2. **`store.append_local`'s seq assignment was a race** spanning an
   `await`; 200 concurrent uploads reliably collided on `(origin, seq)`.
   Found only once the load test fired genuinely concurrent traffic.
3. **Ring identity must be the URL peers already use**, not `NODE_ID` —
   otherwise every node computes a different ring for the same cluster.
4. **A one-sided partition self-heals**; isolating one node in a 3-node
   cluster takes 4 directional calls, not 1.
5. **CLIP zero-shot with one prompt per class has a class prior that beats
   the signal** — one film stock won all 18 reference frames, including
   near-black ones. Fixed with mean-centring plus prompt ensembling.
6. **The subject detector inverted the guidance near a thirds line.**
   Connected-component picking looked right but, because saliency answers
   on edges and background drags the centroid centre-ward, subjects at
   x=0.22 and x=0.72 were reported at 0.40 and 0.53 — across the thirds
   line, so the arrow pointed the wrong way. Fixed with a
   saliency-weighted centroid over the top few percent of mass; error
   against known positions is now ≤1px per axis.
7. **Two overlay elements rendered underneath the camera's own chrome** —
   the arrows behind the film strip, later the reason banner behind the
   exposure readout. Both "passed" checks that read the DOM, which cannot
   see occlusion. The rule now: only things that must align with frame
   coordinates live inside the viewfinder; text pinned to its edges lands
   under chrome positioned against the screen.

Items 6 and 7 share a lesson worth stating plainly: a test that exercises
only the easy case will pass while the feature is broken. The inverted
arrow survived a browser check because that test's subject sat far enough
off-centre to escape the bias.

## Known simplifications

- One "room" — the backend has no multi-event concept.
- `client-2`'s outbox is localStorage, not `client/`'s Dexie + service
  worker.
- Saliency is classical CV, not a semantic detector: it finds visually
  salient regions, so in a dim venue it may box a bright light rather than
  a guest. Real, but not reliably *about the person*.
- The saved photo is the full sensor frame while guidance describes the
  ratio-cropped view; the crop rectangle is advisory.
- The front camera's mirror maths is verified in code but has not been
  pointed at a real face.
- No process-kill endpoint, no Prometheus/Grafana, no fourth consensus
  mechanism — all deliberate, see ROADMAP.md's "What NOT to add".
