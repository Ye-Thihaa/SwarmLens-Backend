"""One-way, append-only sync of the event log to a cloud REST endpoint
(Supabase/PostgREST by convention), running only on whichever node is
Raft leader. Deliberately reuses store.events_missing_from() -- the exact
primitive gossip already uses for anti-entropy -- treating "the cloud" as
one more peer whose digest we track, except the sync only pushes (no
pull-back) and the "digest" is this node's own best-effort local guess,
not something the cloud reports back to us.

Why a best-effort *local* guess is still correct, not just convenient:
the destination table is the idempotency boundary, exactly like store.py's
own `INSERT OR IGNORE` -- it needs a UNIQUE(origin, seq) constraint and
gets `Prefer: resolution=ignore-duplicates` on every push. If Raft
leadership moves to a different node mid-stream, the new leader's local
checkpoint (store.get_meta, backed by local_meta -- never gossiped, see
store.py) won't know what the old leader already pushed. It just resends
from scratch, and the destination silently dedups the overlap. Correct,
if wasteful once. This is the same lesson CLAUDE.md's recap gotcha
teaches: don't gate exactly-once behavior on local, non-replicated state
that can't survive a leadership change -- gate it on the destination's
own idempotency instead.

Setup for a real Supabase project: create a table (default name `events`,
override with SUPABASE_EVENTS_TABLE) with columns matching the event
shape (origin text, seq int8, kind text, payload jsonb, created_at
float8, vclock jsonb) and a UNIQUE(origin, seq) constraint -- that
constraint is what makes `resolution=ignore-duplicates` safe. Set
SUPABASE_URL to the project's REST base (e.g.
https://xxxx.supabase.co) and SUPABASE_KEY to a service-role or
appropriately-scoped key. Unset (default), cloud sync is fully disabled
-- .enabled is False, the loop never even starts, zero effect on any
node that doesn't configure it.
"""
import asyncio
import os
import time

import httpx

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SUPABASE_TABLE = os.getenv("SUPABASE_EVENTS_TABLE", "events")
SYNC_INTERVAL = float(os.getenv("CLOUD_SYNC_INTERVAL", "15.0"))
SYNC_META_KEY = "cloud_synced_digest"
# Real Supabase is external, so this client must honor a system/corporate
# proxy by default (trust_env=True) -- unlike every other httpx client in
# this codebase, which only ever talks to 127.0.0.1 and hardcodes
# trust_env=False. test_cloud_sync.py points SUPABASE_URL at a *local*
# fake-cloud server though, so on a machine whose proxy doesn't exempt
# loopback traffic, that would break the test in the same way it broke
# everything else -- hence this override, defaulted to the correct
# production behavior and only ever flipped by the test harness.
TRUST_ENV = os.getenv("CLOUD_SYNC_TRUST_ENV", "true").lower() != "false"


class CloudSync:
    def __init__(self, node_id: str, store, raft, interval: float = SYNC_INTERVAL,
                 url: str = SUPABASE_URL, key: str = SUPABASE_KEY, table: str = SUPABASE_TABLE):
        self.node_id = node_id
        self.store = store
        self.raft = raft
        self.interval = interval
        self.url = url
        self.key = key
        self.table = table
        self.running = False
        self.last_attempt_at = 0.0
        self.last_synced_count = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def start(self):
        self.running = True
        if self.enabled:
            asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False

    async def _loop(self):
        async with httpx.AsyncClient(timeout=10.0, trust_env=TRUST_ENV) as client:
            while self.running:
                await asyncio.sleep(self.interval)
                await self.sync_once(client)

    async def sync_once(self, client: httpx.AsyncClient) -> int:
        """Push everything this node has that the cloud doesn't, per our
        own local checkpoint. Only the Raft leader does this -- not
        because a follower pushing would be *unsafe* (the destination
        dedups on (origin, seq) same as everywhere else), just because
        there's no reason for three nodes to all push the same backlog
        every tick. Returns the number of events pushed (0 on no-op,
        including "cloud unreachable" -- that's recorded in last_error,
        not raised, so a caller like /cloud_sync/trigger never 500s just
        because the network is down)."""
        self.last_attempt_at = time.time()
        if not self.enabled or self.raft.role != "leader":
            return 0

        last_digest = await self.store.get_meta(SYNC_META_KEY, {})
        outgoing = await self.store.events_missing_from(last_digest)
        if not outgoing:
            self.last_error = None
            return 0

        try:
            await self._push(client, outgoing)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return 0

        # Only advance the checkpoint after a confirmed successful push --
        # if _push raised, last_digest is untouched and the same backlog
        # (plus whatever's accumulated since) is retried next tick.
        await self.store.set_meta(SYNC_META_KEY, await self.store.digest())
        self.last_synced_count += len(outgoing)
        self.last_error = None
        return len(outgoing)

    async def _push(self, client: httpx.AsyncClient, events: list[dict]):
        """image_base64 is stripped from any legacy photo event on the way
        out. This table is a metadata archive -- Postgres jsonb is the
        wrong storage class for a 200KB image, and every such row would be
        TOASTed, slow to query, and duplicated against bytes that already
        have a proper home in object storage (blob_archive.py). Photos
        written since the blob split carry only a blob_hash and are
        unaffected; this keeps the pre-split ones from poisoning the
        archive with pixels."""
        rows = [
            {
                "origin": e["origin"],
                "seq": e["seq"],
                "kind": e["kind"],
                "payload": (
                    {k: v for k, v in e["payload"].items() if k != "image_base64"}
                    if e["kind"] == "photo" else e["payload"]
                ),
                "created_at": e["created_at"],
                "vclock": e.get("vclock") or {},
            }
            for e in events
        ]
        r = await client.post(
            f"{self.url}/rest/v1/{self.table}",
            json=rows,
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=ignore-duplicates,return=minimal",
            },
        )
        r.raise_for_status()

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "role": self.raft.role,
            "table": self.table,
            "interval": self.interval,
            "last_attempt_at": self.last_attempt_at,
            "last_synced_count": self.last_synced_count,
            "last_error": self.last_error,
        }
