# SwarmLens guest client

Phase 5's guest-facing half (see [ROADMAP.md](../ROADMAP.md)): offline-first
photo capture with an IndexedDB outbox and vector clocks, talking to the
3-node backend in this repo. A functional reference implementation proving
the offline-queue + causal-ordering mechanism works end to end -- not the
polished, branded guest UI (that's a separate, not-yet-attached frontend;
see the root [CLAUDE.md](../CLAUDE.md)'s Current status).

## Stack

- **React 19 + TypeScript**, built with **Vite**
- **Dexie.js** (`src/db.ts`) -- the offline `outbox` table:
  `{local_id, kind, payload fields, vclock, blob, synced}`
- **`localStorage`** (`src/vclock.ts`) -- this device's `{device_id:
  counter}` vector clock, attached to every event it creates
- **Service worker + Background Sync API** (`src/sw.ts`,
  `vite-plugin-pwa` in `injectManifest` mode) -- retries the outbox when
  connectivity returns. Background Sync itself is Chromium-only; the
  primary, cross-browser retry path is the foreground `online` listener +
  10s poll in `OutboxStatus.tsx` -- Background Sync is a progressive
  enhancement on top of that, not a replacement for it.
- **`getUserMedia` + canvas**, not the native file-picker `<input
  capture>` -- a live in-app viewfinder + shutter, closer to a real camera
  app and matching ROADMAP.md's "take 5 photos" test flow.
- No state management library, no component library, no axios -- the app
  is small enough that Dexie's `useLiveQuery` + local component state +
  `fetch` cover everything without the extra dependency weight.

## Run it

The backend cluster must already be running (see the root
[README.md](../README.md)):

```bash
npm install
npm run dev
```

Defaults to `http://localhost:8001,8002,8003`. Override with
`VITE_NODE_URLS=http://host:port,...` in a `.env.local` if your cluster
runs elsewhere.

`npm run build` type-checks and builds the production bundle (including
the real service worker via `vite-plugin-pwa`'s `injectManifest`
strategy) into `dist/`.

## How it fits together

- **Capture** (`CaptureScreen.tsx`): shutter press writes to the outbox
  immediately and renders optimistically -- it never waits on the
  network. `sync.ts`'s `syncOutbox()` is then triggered in the
  background, fire-and-forget.
- **Sync** (`sync.ts`, `nodes.ts`): for each unsynced outbox row, races a
  health check against every configured node ("nearest edge node" without
  real geo-routing infrastructure) and POSTs to whichever answers first.
  A node being unreachable is treated as "stay queued," never as an
  error -- going offline is routine, not exceptional, for this app.
- **Gallery** (`Gallery.tsx`): polls `GET /photos` on a reachable node.
  This backend has no photo-binary storage of its own (see the root
  `ai_engine.py`'s `/analyze` docstring) -- only this device's own
  captures have real image bytes available locally, so remote entries
  render as a metadata-only card (zone, guest, likes) rather than a
  broken image. Photos are grouped by `concurrent_with` (an adjacency
  the backend already computes from vector-clock comparison in
  `main.py`'s `_concurrent`) into clusters rendered together with a
  badge, instead of a gallery that implies a false total order from
  network arrival timing.

## What this isn't

Not the actual branded/designed guest UI (AR reframe overlay, film-stock
picker auto-selection from `POST /analyze`, etc.) -- that's a separate
frontend the project has elsewhere, not attached to this working
directory. This client proves the offline+causal-ordering mechanism
works; wiring it into that real design is separate work once that repo
is available.
