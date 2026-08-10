import asyncio
import base64
import hashlib
import json
import os
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

-- Photo bytes, content-addressed, deliberately NOT in `events`.
--
-- They used to live inline in the photo event's payload as base64, which
-- made photos 99.8% of the log by size (~203KB/photo) and broke three
-- things at event scale: store.photos() read every byte off disk on every
-- poll only to discard it, a gossip batch of 500 photo events built a
-- ~100MB HTTP body no 3s timeout could transfer, and cloud_sync pushed
-- 200KB base64 blobs into Postgres jsonb. Splitting them out leaves the
-- log as pure metadata (~1KB/photo) that still gossips and replays exactly
-- as before, and moves the bytes onto their own out-of-band replication
-- path (blob_sync.py) with a byte budget.
--
-- Keyed by sha256 of the raw bytes, so the same image uploaded twice (a
-- retry, two devices) stores once. `archived_url` is set once the bytes
-- are safely in object storage.
--
-- Note there is deliberately no `pinned` column. Which blobs a frozen
-- recap depends on is DERIVED from the recap events (store.pinned_hashes)
-- rather than flagged here -- a local flag only ever got set on whichever
-- node was leader when the recap fired, so the three nodes disagreed
-- about what was safe to evict. Same convention as every other derived
-- read in this file.
CREATE TABLE IF NOT EXISTS blobs (
    hash         TEXT PRIMARY KEY,
    bytes        BLOB NOT NULL,
    size         INTEGER NOT NULL,
    mime         TEXT NOT NULL DEFAULT 'image/jpeg',
    archived_url TEXT,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_origin_seq ON events(origin, seq);
CREATE INDEX IF NOT EXISTS idx_events_kind       ON events(kind);
"""

# Which event a photo belongs to when nothing said. Every photo logged
# before multi-event hosting existed has no event_id in its payload, and
# every client that doesn't send one still works -- both land here rather
# than in a nameless bucket no read can reach. A single-event deployment
# never has to think about this field at all.
DEFAULT_EVENT_ID = "default"

# Byte ceiling on one gossip/cloud-sync batch. See events_missing_from:
# the row cap it sits beside is not a size cap, and with any large payload
# a 500-row batch builds a body no 3-second timeout can transfer.
MAX_SYNC_BYTES = int(os.getenv("MAX_SYNC_BYTES", str(4 * 1024 * 1024)))


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

    async def events_missing_from(self, peer_digest: dict[str, int],
                                  limit: int = 500,
                                  max_bytes: int = MAX_SYNC_BYTES):
        """Every event we hold that the peer's digest says it has not seen,
        bounded by BOTH a row count and a byte budget.

        The row cap alone was never a size cap. With photo bytes inline
        (pre-blob-split, and still true of any legacy row), 500 events
        meant a ~100MB JSON body against gossip's 3-second client timeout
        -- so a node that fell behind could never catch up, because every
        attempt to catch up timed out. Stopping at max_bytes instead just
        means this round syncs a prefix and the next round continues from
        the advanced digest; anti-entropy is already incremental, so a
        partial batch is a normal outcome, not a failure.

        Always yields at least one event once there's anything to send,
        even if that single event exceeds the budget -- otherwise one
        oversized row would wedge the origin it belongs to forever."""
        mine = await self.digest()
        out = []
        spent = 0
        for origin, hi in sorted(mine.items()):
            theirs = peer_digest.get(origin, 0)
            if hi <= theirs:
                continue
            cur = await self.db.execute(
                "SELECT * FROM events WHERE origin=? AND seq>? ORDER BY seq LIMIT ?",
                (origin, theirs, limit),
            )
            for r in await cur.fetchall():
                if spent >= max_bytes and out:
                    return out
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
                spent += len(r["payload"])
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
        photo_image()) -- one choke point, not a flag checked
        separately in each caller."""
        cur = await self.db.execute(
            "SELECT DISTINCT json_extract(payload,'$.photo_id') AS pid FROM events WHERE kind='photo_delete'"
        )
        return {r["pid"] for r in await cur.fetchall()}

    # ---- blobs: photo bytes, content-addressed, outside the event log ----

    async def put_blob(self, raw: bytes, mime: str = "image/jpeg") -> str:
        """Store raw image bytes, keyed by their own sha256. Returns the
        hash to put in the photo event's payload. Idempotent by
        construction: the same bytes always produce the same key, so a
        retry or a second device uploading an identical frame stores once
        rather than twice. INSERT OR IGNORE rather than REPLACE so an
        existing row's pinned/archived_url flags survive a re-upload --
        re-posting a photo that some recap already pinned must not quietly
        unpin it."""
        digest = hashlib.sha256(raw).hexdigest()
        await self.db.execute(
            "INSERT OR IGNORE INTO blobs (hash, bytes, size, mime, created_at) "
            "VALUES (?,?,?,?,?)",
            (digest, raw, len(raw), mime, time.time()),
        )
        await self.db.commit()
        return digest

    async def get_blob(self, blob_hash: str) -> tuple[bytes, str] | None:
        """Raw bytes + mime for one blob, or None if this node doesn't hold
        it. None is a routine answer, not an error: blobs replicate
        out-of-band and lazily (see blob_sync.py), so a node can legitimately
        know a photo's metadata before it has the pixels."""
        cur = await self.db.execute(
            "SELECT bytes, mime FROM blobs WHERE hash=?", (blob_hash,)
        )
        row = await cur.fetchone()
        return (row["bytes"], row["mime"]) if row else None

    async def has_blob(self, blob_hash: str) -> bool:
        cur = await self.db.execute("SELECT 1 FROM blobs WHERE hash=? LIMIT 1", (blob_hash,))
        return await cur.fetchone() is not None

    async def blob_hashes(self) -> set[str]:
        """Every blob hash this node holds -- the digest a peer compares
        against to work out what to send. Hashes only, never bytes: this
        crosses the wire every blob-sync round."""
        cur = await self.db.execute("SELECT hash FROM blobs")
        return {r["hash"] for r in await cur.fetchall()}

    async def pinned_hashes(self) -> set[str]:
        """Blob hashes some frozen recap depends on: replicated first,
        archived to object storage, never evicted by retention.

        Derived from the recap events themselves, NOT stored as a flag on
        the blob row. A flag would be a second source of truth and a wrong
        one: the recap is a replicated event, so every node must reach the
        same answer, but a local UPDATE only ever ran on whichever node
        happened to be leader at the moment it fired -- and only for blobs
        that node had already pulled. Every other derived read here works
        this way for the same reason (see the Conventions note in
        CLAUDE.md); pinning is not the place to start making exceptions."""
        cur = await self.db.execute(
            "SELECT payload FROM events WHERE kind='recap_sent'"
        )
        out: set[str] = set()
        for r in await cur.fetchall():
            for p in json.loads(r["payload"]).get("photos", []):
                if p.get("blob_hash"):
                    out.add(p["blob_hash"])
        return out

    async def wanted_blob_hashes(self) -> list[str]:
        """Blob hashes the log says exist, that this node doesn't hold --
        what blob_sync should go and fetch, pinned (recap) ones first.

        Derived from the event log rather than from a replicated blob
        table, for the usual reason: the log is the one source of truth
        gossip already converges, so a node learns what pixels it's
        missing by the same mechanism it learns everything else."""
        cur = await self.db.execute(
            "SELECT DISTINCT json_extract(payload,'$.blob_hash') AS h "
            "FROM events WHERE kind='photo' AND h IS NOT NULL"
        )
        wanted = {r["h"] for r in await cur.fetchall()}

        # Recap-pinned hashes are unioned in, not just used for ordering.
        # A recap can name a blob that NO photo event mentions: a photo
        # predating the blob split carries its bytes inline and no hash, so
        # when the freeze materialises one (ensure_blob_for_photo) the only
        # record of that hash is the recap event itself. Deriving purely
        # from photo events left exactly those blobs -- the ones a recap
        # promised to keep -- stranded on the single node that made them.
        pinned = await self.pinned_hashes()
        wanted |= pinned
        if not wanted:
            return []
        have = await self.blob_hashes()
        missing = [h for h in wanted if h not in have]
        missing.sort(key=lambda h: (h not in pinned, h))
        return missing

    async def pinned_unarchived(self) -> list[str]:
        """Recap-pinned blobs this node holds that aren't yet safely in
        object storage -- what blob_archive.py uploads. Intersects the
        log-derived pinned set (see pinned_hashes) against rows actually
        present locally: a blob pinned by a recap whose bytes this node
        hasn't pulled yet has nothing to upload, and blob_sync is already
        fetching it as a priority."""
        pinned = await self.pinned_hashes()
        if not pinned:
            return []
        cur = await self.db.execute(
            "SELECT hash FROM blobs WHERE archived_url IS NULL ORDER BY created_at"
        )
        return [r["hash"] for r in await cur.fetchall() if r["hash"] in pinned]

    async def mark_archived(self, blob_hash: str, url: str) -> None:
        """Record that a blob's bytes are safe off-cluster. Node-local and
        non-authoritative on purpose: it's a cache of 'we know this is
        uploaded', and the worst case of losing it is re-uploading
        identical bytes to the same content-addressed key."""
        await self.db.execute(
            "UPDATE blobs SET archived_url=? WHERE hash=?", (url, blob_hash)
        )
        await self.db.commit()

    async def blob_stats(self, with_pinned: bool = False) -> dict:
        """Counts only, by default. The pinned figures need the recap
        replay plus a full hash scan, and this is on GET /health -- polled
        every second by the dashboard and the operator console, from three
        nodes. Putting an O(recaps + blobs) walk on that path would be the
        same event-loop pressure the json_remove work in photos() was
        written to remove. /blob_archive/status asks for the full picture;
        /health doesn't need it."""
        cur = await self.db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS b, "
            "COUNT(archived_url) AS a FROM blobs"
        )
        row = await cur.fetchone()
        stats = {"blobs": row["n"], "bytes": row["b"], "archived": row["a"]}
        if with_pinned:
            held = await self.blob_hashes()
            pinned = await self.pinned_hashes()
            # Pinned blobs actually held here. The log-derived pinned set
            # can name blobs this node hasn't pulled yet, and reporting
            # those as present would overstate what's safe locally.
            stats["pinned"] = len(pinned & held)
            stats["pinned_total"] = len(pinned)
        return stats

    async def photos(self, event_id: str | None = None) -> list[dict]:
        """Every photo this node holds, or just one event's when event_id is
        given. None means "all events merged", which is what every caller
        predating multi-event hosting (job scanning, /zones with no
        event_id, load_test.py, dashboard.html) still asks for and still
        gets. The photo event is the only kind that carries an event_id:
        likes, aesthetic scores, public marks and delete tombstones all
        reference a globally-unique photo_id and inherit that photo's
        event, so there is exactly one place an event membership is
        recorded and no second one to disagree with it.

        json_remove strips a legacy photo's inline image_base64 *inside
        SQLite*, before the row ever crosses into Python. New photos carry
        only a blob_hash so there's nothing to strip, but events written
        before the blob split still hold ~200KB of base64 each, and
        json.loads-ing that only to pop it made this endpoint read 9.68MB
        off disk to return 5.8KB -- 99.9% waste, ~52ms per 50 photos, all
        of it synchronous work on the same asyncio loop raft's 50ms
        heartbeat runs on. At event scale that alone was enough to get a
        healthy leader voted out.

        Like counts come from one grouped query rather than a per-photo
        call, for the same reason: N+1 across a few thousand photos is
        thousands of round trips inside one request."""
        deleted = await self.deleted_photo_ids()
        likes = await self.like_counts()
        cur = await self.db.execute(
            "SELECT origin, created_at, vclock, "
            "json_remove(payload,'$.image_base64') AS payload "
            "FROM events WHERE kind='photo' ORDER BY created_at"
        )
        photos = []
        for r in await cur.fetchall():
            p = json.loads(r["payload"])
            if p["photo_id"] in deleted:
                continue
            p.setdefault("event_id", DEFAULT_EVENT_ID)
            if event_id is not None and p["event_id"] != event_id:
                continue
            p["likes"] = likes.get(p["photo_id"], 0)
            p["stored_on"] = r["origin"]
            p["vclock"] = json.loads(r["vclock"])
            p["taken_at"] = r["created_at"]  # already on the event, just not exposed until now
            photos.append(p)
        return photos

    async def photo_image(self, photo_id: str) -> tuple[bytes, str] | None:
        """Raw image bytes + mime for one photo, from whichever of the two
        storage generations it belongs to:

          - blob_hash in the payload (current): bytes live in the blobs
            table, replicated out-of-band by blob_sync.py. Returns None if
            this node knows the photo but hasn't pulled its pixels yet --
            a routine, temporary state, and the caller's cue to try a peer
            rather than to 404 permanently.
          - image_base64 in the payload (legacy, pre-blob-split): decoded
            straight from the event. Kept working indefinitely; there are
            real photos in real databases shaped this way and rewriting
            history to migrate them would be a worse trade than a branch
            here.

        None also when photo_delete tombstoned it -- "deleted everywhere"
        has to mean the bytes too, not just the listing."""
        if photo_id in await self.deleted_photo_ids():
            return None
        cur = await self.db.execute(
            "SELECT json_extract(payload,'$.blob_hash') AS blob_hash, "
            "json_extract(payload,'$.mime') AS mime, "
            "json_extract(payload,'$.image_base64') AS legacy_b64 "
            "FROM events WHERE kind='photo' "
            "AND json_extract(payload,'$.photo_id')=? LIMIT 1",
            (photo_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        if row["blob_hash"]:
            return await self.get_blob(row["blob_hash"])
        if row["legacy_b64"]:
            try:
                return base64.b64decode(row["legacy_b64"], validate=True), "image/jpeg"
            except Exception:
                return None
        return None

    async def blob_hash_for_photo(self, photo_id: str) -> str | None:
        """Which blob a photo's pixels live in, if it's a post-split photo.
        None for a legacy inline photo -- see ensure_blob_for_photo."""
        cur = await self.db.execute(
            "SELECT json_extract(payload,'$.blob_hash') AS h FROM events "
            "WHERE kind='photo' AND json_extract(payload,'$.photo_id')=? LIMIT 1",
            (photo_id,),
        )
        row = await cur.fetchone()
        return row["h"] if row else None

    async def ensure_blob_for_photo(self, photo_id: str) -> str | None:
        """A photo's blob hash, materializing one from legacy inline bytes
        if that's all this photo has. Used by the recap freeze.

        Legacy photos carry base64 in the event and no blob_hash, so
        nothing downstream of a hash could reach them: they could never be
        pinned, and therefore never archived to object storage. That
        stopped being merely a missing feature once cloud_sync began
        stripping image_base64 on the way out -- at that point a pre-split
        photo had no off-cluster copy at all, which is a regression for
        photos that already exist.

        The event itself is NOT rewritten. It's already been gossiped
        under its (origin, seq), and mutating a payload locally would make
        two nodes disagree about the content of the same event -- the one
        thing the merge model assumes can't happen. Instead the bytes are
        decoded into the blob store here, and the hash is recorded in the
        *recap* event, which is new and can legitimately carry it. The
        photo event stays exactly as written; the recap gains a durable
        handle on its pixels."""
        existing = await self.blob_hash_for_photo(photo_id)
        if existing:
            return existing
        found = await self.photo_image(photo_id)
        if found is None:
            return None
        raw, mime = found
        return await self.put_blob(raw, mime)

    async def archived_url_for(self, blob_hash: str) -> str | None:
        """Where a blob was uploaded, if it has been. The last link in
        GET /photos/{id}/image's fallback chain (local -> peer -> archive),
        which is what makes the archive readable rather than write-only."""
        cur = await self.db.execute(
            "SELECT archived_url FROM blobs WHERE hash=?", (blob_hash,)
        )
        row = await cur.fetchone()
        return row["archived_url"] if row else None

    async def like_counts(self) -> dict[str, int]:
        """Every photo's like count in one grouped query -- the batch form
        of like_count below, so photos() doesn't fire one query per photo.
        Same set-union CRDT semantics: DISTINCT guest_id, so a guest
        liking twice still counts once."""
        cur = await self.db.execute(
            "SELECT json_extract(payload,'$.photo_id') AS pid, "
            "COUNT(DISTINCT json_extract(payload,'$.guest_id')) AS n "
            "FROM events WHERE kind='like' GROUP BY pid"
        )
        return {r["pid"]: r["n"] for r in await cur.fetchall()}

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
        client sends alongside a publish toggle. json_remove drops a legacy
        photo's inline bytes inside SQLite -- see photos() for why doing it
        after json.loads is the expensive way round."""
        cur = await self.db.execute(
            "SELECT json_remove(payload,'$.image_base64') AS payload FROM events "
            "WHERE kind='photo' AND json_extract(payload,'$.photo_id')=? LIMIT 1",
            (photo_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        p = json.loads(row["payload"])
        p.setdefault("event_id", DEFAULT_EVENT_ID)
        return p

    async def events_catalog(self) -> dict[str, dict]:
        """event_id -> its latest event_created payload (name, venue, zones,
        join_token). Replayed from the log like every other derived read,
        so editing an event is just another append and gossip carries it to
        every node with no new mechanism. Last write wins by created_at --
        same reasoning as public_state: two operators renaming one event
        should converge on whichever rename happened last, not merge."""
        cur = await self.db.execute(
            "SELECT payload, created_at FROM events WHERE kind='event_created' ORDER BY created_at"
        )
        out: dict[str, dict] = {}
        for r in await cur.fetchall():
            p = json.loads(r["payload"])
            # First registration wins the slug (see main.py's
            # resolve_event), so carry the *original* timestamp forward
            # across edits rather than letting a rename jump an event
            # ahead of one that claimed the slug before it.
            p["created_at"] = out.get(p["event_id"], {}).get("created_at", r["created_at"])
            out[p["event_id"]] = p
        return out

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
        answer.

        image_base64 is stripped: zone scoring needs a photo's zone and
        event, never its pixels, and this response crosses the wire to
        every node a quorum read samples. Shipping the bytes made every
        /zones/quorum call transfer the whole gallery between nodes --
        survivable while one event existed, not once a node holds several
        events' photos at once. Stripped in SQL (json_remove) rather than
        after json.loads, so a legacy photo's 200KB of base64 is never
        parsed into Python just to be discarded -- see photos()."""
        placeholders = ",".join("?" for _ in kinds)
        cur = await self.db.execute(
            f"SELECT origin, seq, kind, created_at, vclock, "
            f"json_remove(payload,'$.image_base64') AS payload "
            f"FROM events WHERE kind IN ({placeholders})", kinds
        )
        out = []
        for r in await cur.fetchall():
            payload = json.loads(r["payload"])
            if r["kind"] == "photo":
                payload.setdefault("event_id", DEFAULT_EVENT_ID)
            out.append(
                {
                    "origin": r["origin"],
                    "seq": r["seq"],
                    "kind": r["kind"],
                    "payload": payload,
                    "created_at": r["created_at"],
                    "vclock": json.loads(r["vclock"]),
                }
            )
        return out

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

    async def recapped_event_ids(self) -> set[str]:
        """Events whose recap has been frozen -- i.e. that have ended. Read
        straight off the log rather than kept as a status field on the
        event, so there's no second source of truth to drift from the
        recap itself."""
        cur = await self.db.execute(
            "SELECT DISTINCT COALESCE(json_extract(payload,'$.event_id'), ?) AS eid "
            "FROM events WHERE kind='recap_sent'",
            (DEFAULT_EVENT_ID,),
        )
        return {r["eid"] for r in await cur.fetchall()}

    async def recap_for(self, event_id: str) -> dict | None:
        """The frozen top-liked snapshot for one hosted event, once its
        recap has fired -- kind='recap_sent', filtered the same way
        photos() filters by event_id (a legacy row with none defaults to
        DEFAULT_EVENT_ID). Doubles as the exactly-once check main.py's
        send_event_recap needs before appending a new one: re-checking the
        replicated log, not local memory, is what makes that guarantee
        survive a leader crash and re-election. Earliest by created_at
        wins in the rare case two would-be leaders both passed that check
        in the same race window -- same first-one-wins reasoning as
        events_catalog's slug clash, not a merge."""
        cur = await self.db.execute(
            "SELECT payload FROM events WHERE kind='recap_sent' ORDER BY created_at"
        )
        for r in await cur.fetchall():
            p = json.loads(r["payload"])
            p.setdefault("event_id", DEFAULT_EVENT_ID)
            if p["event_id"] == event_id:
                return p
        return None

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
