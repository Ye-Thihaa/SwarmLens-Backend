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

    async def append_local(self, kind: str, payload: dict) -> dict:
        """Record an event this node originated. Assigns the next local sequence."""
        seq = await self._next_seq()
        event = {
            "origin": self.node_id,
            "seq": seq,
            "kind": kind,
            "payload": payload,
            "created_at": time.time(),
        }
        await self.db.execute(
            "INSERT INTO events (origin, seq, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (event["origin"], seq, kind, json.dumps(payload), event["created_at"]),
        )
        await self.db.commit()
        return event

    async def merge_remote(self, events: list[dict]) -> int:
        """Absorb events learned from a peer. Idempotent: replays are ignored."""
        if not events:
            return 0
        rows = [
            (e["origin"], e["seq"], e["kind"], json.dumps(e["payload"]), e["created_at"])
            for e in events
        ]
        cur = await self.db.executemany(
            "INSERT OR IGNORE INTO events (origin, seq, kind, payload, created_at) "
            "VALUES (?,?,?,?,?)",
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
                    }
                )
        return out

    # ---- derived read models: rebuilt from the log, never written directly ----

    async def photos(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM events WHERE kind='photo' ORDER BY created_at"
        )
        photos = []
        for r in await cur.fetchall():
            p = json.loads(r["payload"])
            p["likes"] = await self.like_count(p["photo_id"])
            p["stored_on"] = r["origin"]
            photos.append(p)
        return photos

    async def like_count(self, photo_id: str) -> int:
        """Set-union CRDT: one guest liking twice still counts once, in any order."""
        cur = await self.db.execute(
            "SELECT COUNT(DISTINCT json_extract(payload,'$.guest_id')) AS n "
            "FROM events WHERE kind='like' AND json_extract(payload,'$.photo_id')=?",
            (photo_id,),
        )
        row = await cur.fetchone()
        return row["n"] or 0

    async def event_count(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM events")
        return (await cur.fetchone())["n"]

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
