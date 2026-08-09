import asyncio
import json
import time
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    origin      TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    payload     TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    vclock      TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (origin, seq)
);

CREATE TABLE IF NOT EXISTS local_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_origin_seq ON events(origin, seq);
CREATE INDEX IF NOT EXISTS idx_events_kind       ON events(kind);
"""


class Store:
    def __init__(self, path: str, node_id: str):
        self.path = path
        self.node_id = node_id
        self.db: aiosqlite.Connection | None = None
        # _next_seq is a read-then-write (SELECT the counter, compute
        # +1, UPDATE it) spanning an `await` -- without this lock, two
        # concurrent append_local calls can both read the same value
        # before either writes back, assign the same seq, and collide on
        # events' (origin, seq) UNIQUE constraint. Real bug, found by
        # load_test.py firing genuinely concurrent requests at one node
        # for the first time -- every prior test/demo only ever awaited
        # one write at a time. merge_remote doesn't need this: it inserts
        # events whose (origin, seq) were already assigned by their
        # origin node, via INSERT OR IGNORE, which is safe under
        # concurrent execution on its own.
        self._write_lock = asyncio.Lock()

    async def open(self):
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.execute(
            "INSERT OR IGNORE INTO local_meta (key, value) VALUES ('local_seq', '0')"
        )
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()

    async def _next_seq(self) -> int:
        cur = await self.db.execute("SELECT value FROM local_meta WHERE key='local_seq'")
        row = await cur.fetchone()
        nxt = int(row["value"]) + 1
        await self.db.execute(
            "UPDATE local_meta SET value=? WHERE key='local_seq'", (str(nxt),)
        )
        return nxt

    async def append_local(self, kind: str, payload: dict, vclock: dict | None = None) -> dict:
        """Record an event this node originated. Assigns the next local
        sequence. vclock is envelope metadata (like origin/seq), not
        payload -- a guest client's {device_id: counter} clock, attached
        to whatever event it sends. Internal/system events (job leases,
        recap, aesthetic_score, ...) have no guest device behind them, so
        they default to {} -- an empty clock carries no causal info and is
        never treated as concurrent with anything (see main.py's
        _concurrent)."""
        vclock = vclock or {}
        async with self._write_lock:
            seq = await self._next_seq()
            event = {
                "origin": self.node_id,
                "seq": seq,
                "kind": kind,
                "payload": payload,
                "created_at": time.time(),
                "vclock": vclock,
            }
            await self.db.execute(
                "INSERT INTO events (origin, seq, kind, payload, created_at, vclock) VALUES (?,?,?,?,?,?)",
                (event["origin"], seq, kind, json.dumps(payload), event["created_at"], json.dumps(vclock)),
            )
            await self.db.commit()
        return event

    async def merge_remote(self, events: list[dict]) -> int:
        """Absorb events learned from a peer. Idempotent: replays are ignored."""
        if not events:
            return 0
        rows = [
            (e["origin"], e["seq"], e["kind"], json.dumps(e["payload"]), e["created_at"],
             json.dumps(e.get("vclock") or {}))
            for e in events
        ]
        cur = await self.db.executemany(
            "INSERT OR IGNORE INTO events (origin, seq, kind, payload, created_at, vclock) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        await self.db.commit()
        return cur.rowcount

    async def digest(self) -> dict[str, int]:
        """Version vector: highest sequence number seen from every origin."""
        cur = await self.db.execute(
            "SELECT origin, MAX(seq) AS hi FROM events GROUP BY origin"
        )
        return {r["origin"]: r["hi"] for r in await cur.fetchall()}

    async def events_missing_from(self, peer_digest: dict[str, int], limit: int = 500):
        """Every event we hold that the peer's digest says it has not seen."""
        mine = await self.digest()
        out = []
        for origin, hi in mine.items():
            theirs = peer_digest.get(origin, 0)
            if hi <= theirs:
                continue
            cur = await self.db.execute(
                "SELECT * FROM events WHERE origin=? AND seq>? ORDER BY seq LIMIT ?",
                (origin, theirs, limit),
            )
            for r in await cur.fetchall():
                out.append(
                    {
                        "origin": r["origin"],
                        "seq": r["seq"],
                        "kind": r["kind"],
                        "payload": json.loads(r["payload"]),
                        "created_at": r["created_at"],
                        "vclock": json.loads(r["vclock"]),
                    }
                )
        return out

    # ---- derived read models: rebuilt from the log, never written directly ----

    async def deleted_photo_ids(self) -> set[str]:
        """photo_ids with a photo_delete tombstone -- a guest retracting
        one of their own *public* photos (see main.py's POST
        /photos/delete). A non-public delete never reaches here at all;
        it's handled purely client-side by splicing the local outbox,
        since the event log never knew about a photo nobody outside this
        guest's own device needed to see yet. Once tombstoned, a photo_id
        is filtered out of every derived read below (photos(),
        photo_image_b64()) -- one choke point, not a flag checked
        separately in each caller."""
        cur = await self.db.execute(
            "SELECT DISTINCT json_extract(payload,'$.photo_id') AS pid FROM events WHERE kind='photo_delete'"
        )
        return {r["pid"] for r in await cur.fetchall()}

    async def photos(self) -> list[dict]:
        deleted = await self.deleted_photo_ids()
        cur = await self.db.execute(
            "SELECT * FROM events WHERE kind='photo' ORDER BY created_at"
        )
        photos = []
        for r in await cur.fetchall():
            p = json.loads(r["payload"])
            if p["photo_id"] in deleted:
                continue
            p.pop("image_base64", None)  # kept out of the list view -- see
            # photo_image_b64 below, which serves it separately so this
            # endpoint (polled every few seconds by the client) doesn't
            # ship every photo's full image bytes on every call.
            p["likes"] = await self.like_count(p["photo_id"])
            p["stored_on"] = r["origin"]
            p["vclock"] = json.loads(r["vclock"])
            p["taken_at"] = r["created_at"]  # already on the event, just not exposed until now
            photos.append(p)
        return photos

    async def photo_image_b64(self, photo_id: str) -> str | None:
        """Raw image bytes for one photo, base64-encoded, straight from the
        event log -- no second storage path, same source of truth gossip
        already replicated everywhere. None if this photo was never
        uploaded with image_base64 (metadata-only test photos, or captures
        from a guest client that predates this field), or if it's been
        tombstoned by photo_delete -- "deleted from everywhere" has to
        include the raw bytes, not just the listing."""
        if photo_id in await self.deleted_photo_ids():
            return None
        cur = await self.db.execute(
            "SELECT payload FROM events WHERE kind='photo' "
            "AND json_extract(payload,'$.photo_id')=? LIMIT 1",
            (photo_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return json.loads(row["payload"]).get("image_base64")

    async def like_count(self, photo_id: str) -> int:
        """Set-union CRDT: one guest liking twice still counts once, in any order."""
        cur = await self.db.execute(
            "SELECT COUNT(DISTINCT json_extract(payload,'$.guest_id')) AS n "
            "FROM events WHERE kind='like' AND json_extract(payload,'$.photo_id')=?",
            (photo_id,),
        )
        row = await cur.fetchone()
        return row["n"] or 0

    async def aesthetic_scores(self) -> dict[str, float]:
        """photo_id -> latest aesthetic_score. Deterministic given the same
        image (CLIP inference), so duplicate events from re-analysis or two
        nodes racing are expected to agree closely -- last-write-wins by
        created_at is fine here, unlike likes which need a CRDT set."""
        cur = await self.db.execute(
            "SELECT payload FROM events WHERE kind='aesthetic_score' ORDER BY created_at"
        )
        out: dict[str, float] = {}
        for r in await cur.fetchall():
            p = json.loads(r["payload"])
            out[p["photo_id"]] = p["score"]
        return out

    async def photo_by_id(self, photo_id: str) -> dict | None:
        """Single photo's own event payload (minus image bytes), straight
        from the log -- used by /photos/public to check the *real* owner
        of a photo server-side rather than trusting whatever guest_id a
        client sends alongside a publish toggle."""
        cur = await self.db.execute(
            "SELECT payload FROM events WHERE kind='photo' "
            "AND json_extract(payload,'$.photo_id')=? LIMIT 1",
            (photo_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        p = json.loads(row["payload"])
        p.pop("image_base64", None)
        return p

    async def public_state(self) -> dict[str, dict]:
        """photo_id -> {"public": bool, "guest_id": str}, replayed from
        public_mark events (a guest opting one of their own photos into
        the cross-guest public gallery, or opting it back out). Last
        write wins by created_at -- same pattern as aesthetic_scores,
        fine here because a guest toggling their own photo (or two of
        their devices racing) is expected to converge on whichever
        toggle actually happened last, not merge as a set the way likes
        do."""
        cur = await self.db.execute(
            "SELECT payload FROM events WHERE kind='public_mark' ORDER BY created_at"
        )
        out: dict[str, dict] = {}
        for r in await cur.fetchall():
            p = json.loads(r["payload"])
            out[p["photo_id"]] = {"public": p["public"], "guest_id": p["guest_id"]}
        return out

    async def raw_events(self, kinds: tuple[str, ...]) -> list[dict]:
        """Every event of the given kinds, undigested. Used by quorum reads,
        which merge several nodes' partial views by union-ing raw events
        (idempotent on (origin, seq), same as gossip) and recomputing --
        not by picking one node's already-aggregated counts as 'the'
        answer."""
        placeholders = ",".join("?" for _ in kinds)
        cur = await self.db.execute(
            f"SELECT * FROM events WHERE kind IN ({placeholders})", kinds
        )
        return [
            {
                "origin": r["origin"],
                "seq": r["seq"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"]),
                "created_at": r["created_at"],
                "vclock": json.loads(r["vclock"]),
            }
            for r in await cur.fetchall()
        ]

    async def event_count(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM events")
        return (await cur.fetchone())["n"]

    async def get_meta(self, key: str, default):
        """Node-local scratch state (like local_seq) -- never gossiped,
        not shared across nodes. Safe only for values that are fine to
        lose or reset on a fresh process (e.g. a sync checkpoint backed by
        an idempotent destination), never for anything correctness
        -critical -- that belongs in the replicated event log instead."""
        cur = await self.db.execute("SELECT value FROM local_meta WHERE key=?", (key,))
        row = await cur.fetchone()
        return json.loads(row["value"]) if row else default

    async def set_meta(self, key: str, value):
        await self.db.execute(
            "INSERT INTO local_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        await self.db.commit()

    async def event_exists(self, kind: str) -> bool:
        """Cluster-wide idempotence check: has any node already logged this
        kind of event? Used for exactly-once actions (e.g. recap_sent)
        where re-checking the replicated log, not local memory, is what
        makes the guarantee survive a leader crash and re-election."""
        cur = await self.db.execute(
            "SELECT 1 FROM events WHERE kind=? LIMIT 1", (kind,)
        )
        return await cur.fetchone() is not None

    # ---- job leases: derived from job_claimed / job_done events ----

    async def job_state(self, photo_id: str) -> dict:
        """Replay the log for one job. This is why leases survive gossip:
        every node computes the same state from the same events, in any order."""
        cur = await self.db.execute(
            "SELECT * FROM events WHERE kind IN ('job_claimed','job_done') "
            "AND json_extract(payload,'$.photo_id')=? ORDER BY created_at",
            (photo_id,),
        )
        state = {"photo_id": photo_id, "status": "pending", "worker": None,
                 "lease_until": 0, "claim_id": None}
        for r in await cur.fetchall():
            p = json.loads(r["payload"])
            if r["kind"] == "job_done":
                state.update(status="done", worker=p["worker"])
            elif r["kind"] == "job_claimed" and state["status"] != "done":
                # last writer wins among claims only if the previous lease expired
                if p["claimed_at"] + p["lease_seconds"] > state["lease_until"] or state["status"] == "pending":
                    state.update(status="claimed", worker=p["worker"],
                                 lease_until=p["claimed_at"] + p["lease_seconds"],
                                 claim_id=p["claim_id"])
        return state

    async def unclaimed_photo_ids(self) -> list[str]:
        photos = await self.photos()
        out = []
        for p in photos:
            js = await self.job_state(p["photo_id"])
            expired = js["status"] == "claimed" and js["lease_until"] < time.time()
            if js["status"] == "pending" or expired:
                out.append(p["photo_id"])
        return out
