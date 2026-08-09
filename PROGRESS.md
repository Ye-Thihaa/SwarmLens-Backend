# SwarmLens — progress report

_Last updated: 9 August 2026._

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
| Live AI compose guidance (pre-shutter) | Merged (PRs #1–#3), verified on a real phone |
| Auto-zoom + exposure controls | Merged (PRs #4–#5), driven by phone testing |
| Guest client `client/` (reference PWA) | Done |
| Branded guest app + operator console `client-2/` | Done, wired to the real cluster |
| Load test + measured numbers | Done |
| Face-first subject detection (YuNet) | Built on `progress-report`, unmerged, not on a handset |
| Eyes-open / smiling (MediaPipe blendshapes) | Built on `progress-report`, unmerged, never seen fire |

Nothing in the roadmap is outstanding. The last two rows are this working
branch (`progress-report`, four commits ahead of `master`); everything
above them is merged. What remains otherwise are documented
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

`ai_engine.py`, pretrained only, nothing trained or fine-tuned here: YuNet
face detection with OpenCV spectral-residual saliency as the fallback,
MediaPipe FaceLandmarker for eyes and smile, CLIP ViT-B/32 (int8 ONNX) for
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

### Faces come first

Saliency answers *"what is visually unusual here"*, which is not the same
question as *"what is this a photo of"*. The phone session below shows
both halves of that at once: it boxed RGB-lit headphones correctly, and it
would have boxed a bright lamp standing beside the guest with exactly the
same confidence. At an event the subject is a person almost every
time, so `find_subject()` now runs YuNet first (~230KB, OpenCV Zoo) and
falls back to saliency only when there is no face — a room, a table, a
view, where saliency is still the right answer. Callers get
`subject_source` so they can tell which one answered, and the guest
overlay relabels the box `FACE` instead of `SUBJECT`.

Two things follow from a face box being a *head* rather than a subject.
The crop targets a smaller share of the frame (`FACE_SUBJECT_SHARE` 0.20
against saliency's 0.38), so a portrait isn't cropped down to a head shot.
And the box is the union of the foreground faces, not the largest one:
composing on one person while cropping their friend out of frame is worse
than not helping at all. Faces under a quarter of the largest one's area
are dropped, so someone twenty feet behind the group doesn't stretch it.

**Eyes-open and smiling** come from MediaPipe FaceLandmarker (~3.7MB),
which runs only once YuNet has already found a face — a no-face frame
never pays its ~10ms. It reads the *blendshapes* (`eyeBlinkLeft/Right`,
`mouthSmile*`) rather than a hand-rolled eye-aspect ratio, because those
are trained 0..1 outputs: the thresholds are then the model's own
calibration instead of numbers fitted to one photo. This is exactly the
capability YuNet's five landmarks could not provide — one point per eye,
its *centre*, which sits in the same place open or shut, so a blink
detector built on them would be inventing a signal that isn't in the data.
Head roll genuinely *is* in the data and is reported: validated by
rotating a real face through known angles, it tracks to within ~0.3–1.5°
out to ±10°, degrading to ~5° at ±20°.

`test_ai_preview.py` covers the preview endpoint directly — six known
subject positions (including 0.28 and 0.72, the modestly-off-centre cases
a centre-biased detector flips), `move_subject_*` and `pan_camera_*`
staying exact opposites, zero events persisted, and the 413/400 input
guards. It was written after both of that endpoint's real bugs had already
reached a phone.

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
- `subject_source` says whether a face or saliency answered, because the
  two are not equally trustworthy about what the photo is *of* — and the
  reframe sizes them differently for the same reason.
- Both eyes must read shut before `EYES CLOSED · SHOOT AGAIN` appears. One
  is a wink or a detection wobble, and asking for a reshoot over that is
  noise.
- `turned` (yaw) is computed and returned but deliberately never shown to
  a guest. It is a coarse proxy — nose offset from the eye midpoint over
  interocular distance — and on the one real face available to check with,
  a roughly-frontal portrait already measured 0.25 against a 0.35 bound.
  Returned as data, withheld as advice.

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

**On an iOS phone**, over the LAN against the real 3-node cluster —
confirmed from a screen recording of the session:

- the camera works (secure context via the dev cert; Safari still labels
  a self-signed cert "Not Secure", which is expected and harmless)
- a `SUBJECT` box lands on the real subject — RGB-lit headphones on a desk
- the composition arrow is **visible**, reading "↑ TILT UP slightly" —
  this is the fix for the arrows previously hidden behind the film strip
- the film-stock call is apt: Cinestill 800T for RGB/tungsten-ish lighting
  in a dim room, with the reason *"800T lets the lights bloom the way
  tungsten night film does"*
- the `AI PICK` badge appears on that stock and the strip auto-loads it

**That run predates face-first detection.** The headphones were found by
saliency, which was the only detector in the build at the time. Nothing on
the `progress-report` branch has been on a handset yet.

Not yet exercised on the phone: the front camera, left/right `PAN`
guidance specifically (the recorded run produced a vertical nudge), and
the entire face path — the `FACE` box, `EYES CLOSED`, `HEAD TILTED`. The
horizontal path is verified in the browser and by unit-level measurement,
not on a handset. `eyes_open=false` and `smiling=true` have never been
observed firing on any device at all: they are verified only by threshold
margin, open eyes measuring ~0.003 against a 0.5 bound.

In the browser, with all three nodes killed, the overlay clears to
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
7. **The composition arrows rendered underneath the film strip on a
   phone.** At 375×812 a badge anchored to the viewfinder's bottom landed
   at y 548–598; the film strip, positioned against the *screen*, spans
   528–600. Same band, and the strip paints later. The check "passed"
   because it read the DOM with `get_page_text`, which cannot see
   occlusion — the text was present, the pixels were not. Fixed by moving
   the guidance to the viewfinder's centre.
8. **The test scripts would have deleted the demo cluster's databases.**
   Every `test_*.py` hardcodes ports 8001-8003 and `./node1.db`–
   `./node3.db`, and deletes those files on the way out. A busy port
   doesn't fail the bind — uvicorn loses the race and the test's synthetic
   events land in real data — and then the cleanup removes the databases
   it never created. Now guarded: `testutil.ensure_safe_to_run()` exits
   with the `mv node*.db .dbbackup/` fix rather than starting.
9. **A cached model object is shared mutable state.** One `FaceDetectorYN`
   serves the whole process, and `/analyze` and `/analyze/preview` both run
   on `asyncio.to_thread` — overlapping in ordinary use, since client-2
   posts `/analyze` the moment a photo syncs while the preview loop is
   still ticking. Two threads reproduced an OpenCV assertion failure and,
   once in 150 runs, zero faces on a frame containing two — which is
   silently the "no face, use saliency" signal rather than an error.
   Locked; `test_face_concurrency.py` fails without it.

Items 6 and 7 share a lesson worth stating plainly: a test that exercises
only the easy case, or that measures the wrong layer, will pass while the
feature is broken. The inverted arrow survived a browser check because
that test's subject sat far enough off-centre to escape the bias; the
invisible arrow survived because the assertion read the DOM rather than
the screen.

## What real-device testing changed (post-merge)

The AI compose work merged in PRs #1–#3. Testing it on an actual phone
then drove four more commits, and the pattern in them is worth keeping:
**three of the four "bugs" were things that worked but couldn't be seen
working.**

- The reason banner collided with the exposure readout and zone picker at
  phone width — the same class of bug as the hidden arrows. Moved to
  `top-[10rem]`, clear of both.
- An **aesthetic-fallback crop** (recommending a framing when no subject
  was found) and a **4s guidance cooldown** were both added, then reverted
  after real use: the fallback gave worse recommendations than silence on
  scenes with people, and the cooldown made live guidance feel broken
  rather than considerate. `preview()` and `compute_reframe()` were
  restored byte-for-byte, verified against identical output on a test
  image.
- **Auto-zoom** replaced them: the viewfinder now zooms itself into the
  recommended crop every tick, evaluating the full frame fresh each time
  so a subject moving closer loosens the crop as readily as a distant one
  tightens it.
- Its first stability gate required two consecutive near-identical reads
  before applying *any* zoom — a bar real hardware never clears
  (autofocus hunting, exposure settling, JPEG noise), so it never fired at
  all. Split into "engage immediately, debounce only changes/releases"
  with a much looser tolerance.
- *"The resolution sucks"* was not resolution: `SHUTTER` defaulted to a
  simulated 1/60 blur, so the camera opened permanently soft.
  `getUserMedia` also requested no resolution constraints, letting
  browsers silently pick 640×480.
- *"Ratio isn't working"* and *"the settings panel is confusing"* were one
  bug: the panel was nearly opaque and sat over the viewfinder, so
  changing ratio or stock while it was open showed no feedback. The
  controls worked; the panel hid the proof.

## Open issues

- The arrow's caption can overlap the subject box when the two land close
  together.
- Front camera and horizontal `PAN` guidance still unverified on a
  handset.
- `eyes_open=false` and `smiling=true` have never been observed firing —
  only the open-eyed, non-smiling case has been exercised, and only by
  threshold margin.
- The face path as a whole (`FACE` box, `EYES CLOSED`, `HEAD TILTED`) is
  unverified on a handset.
- `progress-report` is four commits ahead of `master` and unmerged. The
  cluster-spinning tests can't run while the demo cluster holds ports
  8001-8003 and the `node*.db` files, by design — see bug 8 above.

## Known simplifications

- One "room" — the backend has no multi-event concept.
- `client-2`'s outbox is localStorage, not `client/`'s Dexie + service
  worker.
- The saliency *fallback* is still classical CV, not a semantic detector:
  it finds visually salient regions, so in a dim venue it may box a bright
  light rather than a guest. Faces now take priority, so this only bites
  when YuNet finds nothing — a profile, a face too small or too dark, or a
  scene with no people in it.
- Yaw is a coarse proxy, not a pose estimate, and only the dominant face
  is ever reported on. In a group shot "someone is turned away" is nearly
  always true, and saying so every frame is noise.
- The saved photo is the full sensor frame while guidance describes the
  ratio-cropped view; the crop rectangle is advisory.
- The front camera's mirror maths is verified in code but has not been
  pointed at a real face.
- No process-kill endpoint, no Prometheus/Grafana, no fourth consensus
  mechanism — all deliberate, see ROADMAP.md's "What NOT to add".
