"""Out-of-band replication for photo bytes.

Photo *metadata* rides the event log and converges by gossip. Photo
*pixels* deliberately do not, and this module is why.

Before the blob split, image bytes lived inline in the photo event as
base64. That made photos 99.8% of the log by size (~203KB each) and broke
gossip outright at event scale: store.events_missing_from() caps a batch
at 500 rows, which with a 203KB payload is a ~100MB JSON body against a
client with a 3-second timeout. A node that fell behind could never catch
up, because every attempt to catch up timed out. The row cap was never a
byte cap.

So the bytes moved here, onto a path with properties gossip can't have:

  - **Byte-budgeted, not row-budgeted.** Each round moves at most
    BLOB_SYNC_BUDGET_BYTES, so a round's cost is bounded no matter how
    many photos landed since the last one. This is the actual fix for the
    100MB-body failure; everything else here follows from it.
  - **One blob per request.** A blob transfer that fails costs one blob,
    not a whole batch, and retries next round with no bookkeeping.
  - **Pinned blobs first.** A frozen recap is meant to outlive its event
    (see main.py's send_event_recap), so the pixels it depends on are the
    ones that most need to exist in more than one place. store.
    wanted_blob_hashes() already returns them first.

Durability is deliberately unchanged from the inline-base64 design: every
node still ends up holding every blob, so the cluster still survives two
node losses. The difference is purely *how the bytes get there* -- steadily
in the background instead of inside a gossip round that had to complete in
one timeout. Read-through (fetch_from_peer, used by GET /photos/{id}/image)
covers the window before the loop catches up, so a guest never sees a
broken image just because replication is still in flight.
"""
import asyncio
import os
import random

import httpx

# How many bytes one sync round may pull. Sized so a round comfortably
# finishes inside BLOB_RPC_TIMEOUT on a LAN even at the low end, and so a
# node that has just joined backfills steadily rather than in one
# unbounded burst that would stall its event loop.
BUDGET_BYTES = int(os.getenv("BLOB_SYNC_BUDGET_BYTES", str(8 * 1024 * 1024)))
INTERVAL = float(os.getenv("BLOB_SYNC_INTERVAL", "2.0"))
# Generous compared to gossip's 3s: this moves real payloads, not digests,
# and unlike gossip nothing time-sensitive is gated on it -- a blob that
# arrives a round late costs nothing, where a gossip round that arrives
# late delays convergence of the log itself.
RPC_TIMEOUT = float(os.getenv("BLOB_RPC_TIMEOUT", "20.0"))


class BlobSync:
    def __init__(self, node_id: str, peers: list[str], store, gossip,
                 interval: float = INTERVAL, budget: int = BUDGET_BYTES):
        self.node_id = node_id
        self.peers = peers
        self.store = store
        # Reuses gossip's failure detector and chaos partitions rather than
        # keeping a second, independently-drifting view of who's up. A
        # partition is meant to isolate a node from its peers, and it would
        # be a strange kind of isolation that still shipped it photos.
        self.gossip = gossip
        self.interval = interval
        self.budget = budget
        self.running = False
        self.rounds = 0
        self.blobs_pulled = 0
        self.bytes_pulled = 0
        self.last_error: str | None = None

    async def start(self):
        self.running = True
        asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False

    def _available_peers(self) -> list[str]:
        alive = set(self.gossip.alive_peers())
        return [p for p in self.peers if p in alive and p not in self.gossip.partitioned]

    async def _loop(self):
        # trust_env=False for the same reason every other internal client
        # in this repo sets it: these are 127.0.0.1 calls, and a system
        # proxy that doesn't exempt loopback turns them into 502s.
        async with httpx.AsyncClient(timeout=RPC_TIMEOUT, trust_env=False) as client:
            while self.running:
                await asyncio.sleep(self.interval)
                try:
                    await self.sync_once(client)
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                self.rounds += 1

    async def sync_once(self, client: httpx.AsyncClient) -> int:
        """Pull as many missing blobs as the byte budget allows. Returns the
        number pulled. Pull-only, no push: every node runs this loop and
        wants the same complete set, so each fetching what it lacks
        converges to the same place as each pushing what a peer lacks --
        with the sender no longer needing to track what anyone else holds."""
        wanted = await self.store.wanted_blob_hashes()
        if not wanted:
            return 0
        peers = self._available_peers()
        if not peers:
            return 0

        spent = 0
        pulled = 0
        for blob_hash in wanted:
            if spent >= self.budget:
                break
            got = await self._fetch(client, blob_hash, peers)
            if got is None:
                continue
            raw, mime = got
            await self.store.put_blob(raw, mime)
            spent += len(raw)
            pulled += 1
            self.blobs_pulled += 1
            self.bytes_pulled += len(raw)
        return pulled

    async def _fetch(self, client, blob_hash: str, peers: list[str]):
        """Try peers in random order until one serves the blob. Random
        rather than fixed order so a 3-node cluster spreads read load
        instead of every node hammering whichever peer sorts first."""
        for peer in random.sample(peers, len(peers)):
            try:
                r = await client.get(f"{peer}/blobs/{blob_hash}")
                if r.status_code == 404:
                    continue  # that peer doesn't have it either
                r.raise_for_status()
                return r.content, r.headers.get("content-type", "image/jpeg")
            except Exception:
                continue
        return None

    async def fetch_from_peer(self, blob_hash: str) -> tuple[bytes, str] | None:
        """One-shot read-through for GET /photos/{id}/image: this node knows
        the photo but hasn't pulled its bytes yet. Fetches, caches locally
        (so the next request is served from disk and the background loop has
        one less blob to move), and returns them.

        Bounded by its own short timeout rather than RPC_TIMEOUT: a guest is
        waiting on this one, and a slow answer is worse than falling back to
        the placeholder the client already renders for a missing image."""
        peers = self._available_peers()
        if not peers:
            return None
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            got = await self._fetch(client, blob_hash, peers)
        if got is None:
            return None
        raw, mime = got
        await self.store.put_blob(raw, mime)
        return raw, mime

    def stats(self) -> dict:
        return {
            "rounds": self.rounds,
            "blobs_pulled": self.blobs_pulled,
            "bytes_pulled": self.bytes_pulled,
            "budget_bytes": self.budget,
            "last_error": self.last_error,
        }
