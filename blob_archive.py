"""Uploads recap-pinned photo bytes to Supabase Storage, so a frozen recap
outlives the cluster that produced it.

The problem this solves is the one the event log can't. Gossip and
blob_sync keep N copies of every blob across N nodes, which survives node
loss but not *fleet* loss: these are three processes on hardware that gets
reimaged, containers that get rebuilt, a venue laptop that goes back in a
cupboard. A wedding recap is expected to still play a year later, and
"still on all three nodes" is not that guarantee. Object storage is.

Deliberately narrow, for the same reason cloud_sync.py is:

  - **Pinned blobs only.** Not every frame every guest shot -- the recap's
    photos, which are the ones with a promise attached. Archiving the
    whole roll is a retention/cost decision nobody has made yet, and
    doing it by default would quietly turn a demo cluster into a
    storage bill.
  - **Leader-gated**, like cloud_sync: three nodes uploading the same
    bytes to the same key is wasted bandwidth, not a correctness problem.
  - **Idempotent on the key**, which IS the correctness boundary. The
    object key is the blob's own content hash, so re-uploading after a
    leadership change overwrites identical bytes with identical bytes.
    Same reasoning as cloud_sync's UNIQUE(origin, seq): let the
    destination be the dedup authority instead of trusting local state
    that can't survive a crash.
  - **Disabled unless configured.** No SUPABASE_URL/SUPABASE_KEY, the loop
    never starts, and nothing about a local cluster changes.

The resulting public URL is recorded on the local blob row
(`archived_url`), which is node-local and non-authoritative on purpose:
it's a cache of "we know this is safe", recomputable by re-uploading, and
therefore exactly the kind of value store.get_meta/local state is safe
for (see cloud_sync.py's docstring on that dividing line).

Setup: create a Storage bucket (default `recap-photos`, override with
SUPABASE_BUCKET). Make it public if you want the archived URLs to be
directly loadable by a guest's browser; keep it private and the URLs
stay a record of what was uploaded rather than a way to fetch it.
"""
import asyncio
import os
import time

import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BUCKET = os.getenv("SUPABASE_BUCKET", "recap-photos")
ARCHIVE_INTERVAL = float(os.getenv("BLOB_ARCHIVE_INTERVAL", "30.0"))
# Same override as cloud_sync's, for the same reason: real Supabase is
# external and must honor a proxy, but the test points this at a local
# fake server that a proxy would break.
TRUST_ENV = os.getenv("CLOUD_SYNC_TRUST_ENV", "true").lower() != "false"


class BlobArchive:
    def __init__(self, node_id: str, store, raft, interval: float = ARCHIVE_INTERVAL,
                 url: str = SUPABASE_URL, key: str = SUPABASE_KEY, bucket: str = BUCKET):
        self.node_id = node_id
        self.store = store
        self.raft = raft
        self.interval = interval
        self.url = url
        self.key = key
        self.bucket = bucket
        self.running = False
        self.last_attempt_at = 0.0
        self.uploaded = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    async def start(self):
        self.running = True
        if self.enabled:
            asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False

    async def _loop(self):
        async with httpx.AsyncClient(timeout=30.0, trust_env=TRUST_ENV) as client:
            while self.running:
                await asyncio.sleep(self.interval)
                await self.archive_once(client)

    async def archive_once(self, client: httpx.AsyncClient) -> int:
        """Upload every pinned-but-unarchived blob this node holds. Returns
        how many were uploaded. Never raises: an unreachable archive is a
        routine condition (no internet at the venue), recorded in
        last_error and retried next tick, not an error that should take
        down a request or the loop."""
        self.last_attempt_at = time.time()
        if not self.enabled or self.raft.role != "leader":
            return 0

        pending = await self.store.pinned_unarchived()
        if not pending:
            self.last_error = None
            return 0

        done = 0
        for blob_hash in pending:
            found = await self.store.get_blob(blob_hash)
            if found is None:
                continue  # pinned by a recap whose bytes we haven't pulled yet
            raw, mime = found
            try:
                url = await self._upload(client, blob_hash, raw, mime)
            except Exception as e:
                # Stop on first failure rather than hammering a dead
                # endpoint once per blob -- the rest retry next tick.
                self.last_error = f"{type(e).__name__}: {e}"
                return done
            await self.store.mark_archived(blob_hash, url)
            self.uploaded += 1
            done += 1

        self.last_error = None
        return done

    async def _upload(self, client: httpx.AsyncClient, blob_hash: str,
                      raw: bytes, mime: str) -> str:
        """PUT rather than POST: PUT upserts in Supabase Storage, so a
        re-upload after a leadership change replaces identical bytes at
        the same key instead of 409ing on an object that's already
        correct."""
        path = f"{self.bucket}/{blob_hash}"
        r = await client.put(
            f"{self.url}/storage/v1/object/{path}",
            content=raw,
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": mime or "image/jpeg",
                "x-upsert": "true",
            },
        )
        r.raise_for_status()
        return f"{self.url}/storage/v1/object/public/{path}"

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "role": self.raft.role,
            "bucket": self.bucket,
            "interval": self.interval,
            "last_attempt_at": self.last_attempt_at,
            "uploaded": self.uploaded,
            "last_error": self.last_error,
        }
