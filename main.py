import asyncio
import base64
import os
import random
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

import ai_engine
from store import Store
from gossip import Gossip
from worker import Worker
from raft import Raft
from hashing import ring_for, owner_of
from cloud_sync import CloudSync, TRUST_ENV as CLOUD_SYNC_TRUST_ENV

QUORUM_EVENT_KINDS = ("photo", "like", "aesthetic_score")

NODE_ID = os.getenv("NODE_ID", "node1")
DB_PATH = os.getenv("DB_PATH", f"./{NODE_ID}.db")
PEERS = [p for p in os.getenv("PEERS", "").split(",") if p]
INTERVAL = float(os.getenv("GOSSIP_INTERVAL", "1.0"))
# Ring identity: peers only ever know this node by its URL (that's what's
# in their PEERS list), never by NODE_ID. If self were keyed by NODE_ID
# while peers are keyed by URL, every node would compute a different
# member set for the same cluster and the ring would disagree with itself
# node to node. SELF_URL must be the address peers reach this node at.
SELF_URL = os.getenv("SELF_URL", "")
SELF_ID = SELF_URL or NODE_ID
N = len(PEERS) + 1   # fixed cluster size (this node + its configured peers)

store = Store(DB_PATH, NODE_ID)
gossip = Gossip(NODE_ID, PEERS, store, INTERVAL)
worker = Worker(NODE_ID, store)


async def send_event_recap():
    """Fires once, cluster-wide, no matter how many times it's triggered
    or how many leaders come and go. Only the leader ever calls this."""
    if await store.event_exists("recap_sent"):
        return
    await store.append_local("recap_sent", {"sent_by": NODE_ID})


raft = Raft(NODE_ID, PEERS)
cloud_sync = CloudSync(NODE_ID, store, raft)


def cluster_members() -> list[str]:
    """Currently alive nodes (self + whatever gossip's failure detector
    still considers reachable). Recomputed fresh on every call, so the
    ring always reflects current membership -- nothing cached to go stale.
    Self is identified by SELF_ID (its URL) to match how peers list it,
    not NODE_ID -- see the SELF_URL comment above."""
    return sorted({SELF_ID, *gossip.alive_peers()})


async def _zone_scores_local() -> dict[str, dict]:
    """Derived read: photos + CRDT like counts (store.photos()) plus the
    average aesthetic_score per zone (store.aesthetic_scores()) for
    whichever photos have been through /analyze so far -- None for a zone
    with no scored photos yet, not 0, so it's distinguishable from 'scored
    and bad'."""
    aesthetic = await store.aesthetic_scores()
    zones: dict[str, dict] = {}
    scored: dict[str, list] = {}
    for p in await store.photos():
        z = zones.setdefault(p["zone"], {"zone": p["zone"], "photos": 0, "likes": 0})
        z["photos"] += 1
        z["likes"] += p["likes"]
        s = aesthetic.get(p["photo_id"])
        if s is not None:
            scored.setdefault(p["zone"], []).append(s)
    for zone, z in zones.items():
        vals = scored.get(zone)
        z["avg_aesthetic"] = round(sum(vals) / len(vals), 2) if vals else None
    return zones


def _rank(zones) -> list[dict]:
    return sorted(zones, key=lambda z: -(z["likes"] * 2 + z["photos"]))


def _zone_scores_from_events(events) -> dict[str, dict]:
    """Same derivation as _zone_scores_local, but over an arbitrary merged
    event set instead of this node's own DB -- what /zones/quorum uses to
    recompute from the union of several nodes' partial views."""
    photo_zone: dict[str, str] = {}
    like_guests: dict[str, set] = {}
    aesthetic: dict[str, float] = {}
    for e in sorted(events, key=lambda e: e["created_at"]):
        p = e["payload"]
        if e["kind"] == "photo":
            photo_zone[p["photo_id"]] = p["zone"]
        elif e["kind"] == "like":
            like_guests.setdefault(p["photo_id"], set()).add(p["guest_id"])
        elif e["kind"] == "aesthetic_score":
            aesthetic[p["photo_id"]] = p["score"]  # last write wins -- sorted by created_at above

    zones: dict[str, dict] = {}
    scored: dict[str, list] = {}
    for photo_id, zone in photo_zone.items():
        z = zones.setdefault(zone, {"zone": zone, "photos": 0, "likes": 0})
        z["photos"] += 1
        z["likes"] += len(like_guests.get(photo_id, ()))
        if photo_id in aesthetic:
            scored.setdefault(zone, []).append(aesthetic[photo_id])
    for zone, z in zones.items():
        vals = scored.get(zone)
        z["avg_aesthetic"] = round(sum(vals) / len(vals), 2) if vals else None
    return zones


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.open()
    await gossip.start()
    await worker.start()
    await raft.start()
    await cloud_sync.start()
    yield
    await cloud_sync.stop()
    await raft.stop()
    await worker.stop()
    await gossip.stop()
    await store.close()


app = FastAPI(title=f"SwarmLens {NODE_ID}", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------------------------- client API ----------------------------

class PhotoIn(BaseModel):
    guest_id: str
    zone: str
    url: str = ""
    composition_score: int = 0
    vclock: dict[str, int] = {}


class LikeIn(BaseModel):
    guest_id: str
    photo_id: str
    vclock: dict[str, int] = {}


@app.post("/photos")
async def upload_photo(body: PhotoIn):
    payload = body.model_dump(exclude={"vclock"})
    payload["photo_id"] = f"ph_{uuid.uuid4().hex[:8]}"
    event = await store.append_local("photo", payload, vclock=body.vclock)
    return {"ok": True, "photo_id": payload["photo_id"], "seq": event["seq"]}


@app.post("/likes")
async def like_photo(body: LikeIn):
    await store.append_local("like", body.model_dump(exclude={"vclock"}), vclock=body.vclock)
    return {"ok": True, "likes": await store.like_count(body.photo_id)}


def _vclock_leq(a: dict, b: dict) -> bool:
    """True if a happened-before-or-equal b in the vector clock partial order."""
    return all(v <= b.get(k, 0) for k, v in a.items())


def _concurrent(a: dict, b: dict) -> bool:
    """True only when both clocks carry real causal info and neither
    dominates the other. An empty clock (no guest device attached one --
    true for every event until a real guest client exists) carries no
    causal info, so it's never flagged concurrent with anything; that
    would be asserting knowledge we don't have."""
    if not a or not b:
        return False
    return not _vclock_leq(a, b) and not _vclock_leq(b, a)


@app.get("/photos")
async def list_photos():
    """Gallery as this node currently sees it. Each photo carries its own
    vclock (empty if the guest client didn't attach one) plus
    concurrent_with: other photo_ids whose vclock is concurrent with this
    one -- neither happened before the other -- so a gallery UI can
    render them side by side instead of implying a false total order
    from arrival time. O(n^2) over this node's photos; fine at demo
    scale, same tradeoff /zones already makes."""
    photos = await store.photos()
    for p in photos:
        p["concurrent_with"] = [
            q["photo_id"] for q in photos
            if q["photo_id"] != p["photo_id"] and _concurrent(p["vclock"], q["vclock"])
        ]
    return {"node": NODE_ID, "photos": photos}


class AnalyzeIn(BaseModel):
    photo_id: str
    image_base64: str | None = None


@app.post("/analyze")
async def analyze_photo(body: AnalyzeIn):
    """Saliency -> AR guide, CLIP -> filter suggestion, CLIP + a linear
    head -> aesthetic score (ai_engine.py; all pretrained, nothing trained
    here). Runs on a thread (ai_engine.analyze is synchronous, CPU-bound
    ONNX/OpenCV work) so it never blocks this node's asyncio loop -- gossip
    and raft both depend on that loop staying responsive.

    image_base64 is the normal path: this backend has no photo-binary
    storage, only zone/composition_score metadata (see PhotoIn), so
    there's nowhere else to get pixels from a bare photo_id unless that
    photo was uploaded with a real, externally-fetchable `url`.

    The aesthetic_score is written with store.append_local exactly like a
    photo or like event -- no second replication path. It gossips, merges
    on (origin, seq), and feeds /zones and /zones/quorum the same way
    everything else in the log already does."""
    if body.image_base64:
        try:
            image_bytes = base64.b64decode(body.image_base64, validate=True)
        except Exception:
            raise HTTPException(400, "image_base64 is not valid base64")
    else:
        photo = next((p for p in await store.photos() if p["photo_id"] == body.photo_id), None)
        if not photo or not photo.get("url"):
            raise HTTPException(
                400, "no image_base64 given, and this photo has no fetchable url"
            )
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            r = await client.get(photo["url"])
            r.raise_for_status()
            image_bytes = r.content

    result = await asyncio.to_thread(ai_engine.analyze, image_bytes)

    await store.append_local("aesthetic_score", {
        "photo_id": body.photo_id,
        "score": result["aesthetic_score"],
        "computed_by": NODE_ID,
    })

    return {
        "ar_guide": result["ar_guide"],
        "suggested_filter": result["suggested_filter"],
        "aesthetic_score": result["aesthetic_score"],
    }


@app.get("/zones")
async def zone_scores():
    """The emergent aesthetic map. Each zone's authoritative score comes
    from whichever node owns it on the consistent-hash ring -- proxied
    live if that's a peer, computed directly if it's us. If the owner is
    unreachable, falls back to this node's own (possibly lagging) replica
    with stale: true rather than failing the request."""
    local = await _zone_scores_local()
    ring = ring_for(cluster_members())

    out = []
    # trust_env=False: `owner` is always a peer from PEERS/SELF_ID, i.e.
    # always 127.0.0.1 -- see the same note in gossip.py for why a system
    # proxy would otherwise break this.
    async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
        for zone, mine in local.items():
            owner = owner_of(zone, ring)
            if owner == SELF_ID:
                out.append({**mine, "owner": owner, "stale": False})
                continue
            try:
                r = await client.get(f"{owner}/zones/local")
                r.raise_for_status()
                remote = {z["zone"]: z for z in r.json()["zones"]}
                if zone in remote:
                    out.append({**remote[zone], "owner": owner, "stale": False})
                    continue
            except Exception:
                pass
            out.append({**mine, "owner": owner, "stale": True})

    return {"node": NODE_ID, "zones": _rank(out)}


@app.get("/zones/local")
async def zones_local():
    """This node's own replica, every zone, no ownership filtering. Not
    for clients -- this is what peers call when proxying a zone they
    don't own, and what a node falls back to reading from itself when the
    owner is unreachable."""
    scores = await _zone_scores_local()
    return {"node": NODE_ID, "zones": _rank(list(scores.values()))}


@app.get("/zones/ring")
async def zones_ring():
    """Debug: current membership and the zone -> owner mapping it implies.
    Useful for demoing how few zones move when a node joins or leaves."""
    members = cluster_members()
    ring = ring_for(members)
    scores = await _zone_scores_local()
    owners = {zone: owner_of(zone, ring) for zone in scores}
    return {"node": NODE_ID, "members": members, "owners": owners}


@app.get("/zones/events")
async def zones_events():
    """Raw photo/like/aesthetic_score events from this node's own replica,
    undigested. Not for clients -- this is what /zones/quorum fetches from
    each node it samples, so it can union events across nodes and
    recompute rather than trust one node's already-aggregated counts."""
    return {"node": NODE_ID, "events": await store.raw_events(QUORUM_EVENT_KINDS)}


@app.get("/zones/quorum")
async def zones_quorum(R: int = 2, W: int = 2):
    """Quorum read: sample R of N nodes (this node is just one candidate
    among them, not privileged), union their raw photo/like events --
    deduped on (origin, seq), the same idempotence key gossip relies on
    -- and recompute scores from the merge. Which R members get sampled
    is random on every call, on purpose: that's what makes 'sometimes you
    land on the stale node' an observable, repeatable demo rather than a
    one-off.

    With R > N/2, any sample is guaranteed to include at least one node
    outside a single-node partition, so the union is always complete.
    With R=1 a sample can land entirely on a partitioned/lagging node and
    return an undercount until it's healed and gossip catches it up.

    W is accepted and echoed back for the CAP-tradeoff narrative only --
    this system's writes are always local + eventually gossiped, there's
    no synchronous quorum write path for W to gate."""
    R = max(1, min(R, N))
    W = max(1, min(W, N))

    members = [SELF_ID] + PEERS
    chosen = random.sample(members, R)

    merged: dict[tuple[str, int], dict] = {}
    queried, unreachable = [], []
    # trust_env=False: `member` is always PEERS/SELF_ID, always
    # 127.0.0.1 -- see the same note in gossip.py.
    async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
        for member in chosen:
            if member == SELF_ID:
                events = await store.raw_events(QUORUM_EVENT_KINDS)
                queried.append(member)
            else:
                try:
                    r = await client.get(f"{member}/zones/events")
                    r.raise_for_status()
                    events = r.json()["events"]
                    queried.append(member)
                except Exception:
                    unreachable.append(member)
                    continue
            for e in events:
                merged[(e["origin"], e["seq"])] = e

    scores = _zone_scores_from_events(merged.values())
    return {
        "node": NODE_ID,
        "N": N,
        "R": R,
        "W": W,
        "strongly_consistent": R + W > N,
        "queried": queried,
        "unreachable": unreachable,
        "zones": _rank(list(scores.values())),
    }


# ---------------------------- gossip API ----------------------------

class SyncIn(BaseModel):
    from_: str = ""
    digest: dict[str, int] = {}

    model_config = {"populate_by_name": True}


@app.post("/gossip/sync")
async def gossip_sync(body: dict):
    """Peer sends its digest; we return our digest plus everything it lacks."""
    peer_digest = body.get("digest", {})
    return {
        "node": NODE_ID,
        "digest": await store.digest(),
        "events": await store.events_missing_from(peer_digest),
    }


@app.post("/gossip/push")
async def gossip_push(body: dict):
    merged = await store.merge_remote(body.get("events", []))
    return {"node": NODE_ID, "merged": merged}


# ---------------------------- raft ----------------------------

class RequestVoteIn(BaseModel):
    term: int
    candidate_id: str


class HeartbeatIn(BaseModel):
    term: int
    leader_id: str


@app.post("/raft/request_vote")
async def raft_request_vote(body: RequestVoteIn):
    return await raft.handle_request_vote(body.term, body.candidate_id)


@app.post("/raft/heartbeat")
async def raft_heartbeat(body: HeartbeatIn):
    return await raft.handle_heartbeat(body.term, body.leader_id)


@app.get("/raft/status")
async def raft_status():
    return raft.status()


@app.post("/recap/trigger")
async def recap_trigger():
    """Fire the one-time 'event recap ready' notification. Safe to call on
    every node (a client doesn't know who the leader is) or more than once
    (idempotent) -- only the leader acts, and only the first time."""
    if raft.role == "leader":
        await send_event_recap()
        return {"triggered": True, "by": NODE_ID}
    return {"triggered": False, "by": NODE_ID}


# ---------------------------- cloud archive sync ----------------------------

@app.post("/cloud_sync/trigger")
async def cloud_sync_trigger():
    """Force a sync tick now instead of waiting for CLOUD_SYNC_INTERVAL --
    mainly for tests/demos. Same no-op rules as the background loop: 0
    synced if this node isn't leader, or if SUPABASE_URL isn't configured
    (cloud_sync.enabled is False), or if the cloud is unreachable (check
    last_error, not an exception -- a network blip here should never 500
    the request)."""
    async with httpx.AsyncClient(timeout=10.0, trust_env=CLOUD_SYNC_TRUST_ENV) as client:
        n = await cloud_sync.sync_once(client)
    return {"synced": n, **cloud_sync.status()}


@app.get("/cloud_sync/status")
async def cloud_sync_status():
    return cloud_sync.status()


# ---------------------------- ops & chaos ----------------------------

@app.get("/jobs/{photo_id}")
async def job_status(photo_id: str):
    return await store.job_state(photo_id)


@app.get("/jobs")
async def all_jobs():
    photos = await store.photos()
    return {p["photo_id"]: await store.job_state(p["photo_id"]) for p in photos}


@app.get("/health")
async def health():
    return {
        "node": NODE_ID,
        "events": await store.event_count(),
        "digest": await store.digest(),
        "gossip": gossip.stats(),
        "raft": raft.status(),
        "cloud_sync": cloud_sync.status(),
    }


@app.post("/chaos/partition/{peer_index}")
async def partition(peer_index: int):
    """Simulate a network split without touching Docker or iptables."""
    peer = PEERS[peer_index]
    gossip.partitioned.add(peer)
    return {"partitioned_from": peer}


@app.post("/chaos/heal")
async def heal():
    gossip.partitioned.clear()
    return {"partitioned_from": []}


@app.get("/dashboard")
async def dashboard():
    """Static, dependency-free chaos + metrics dashboard (dashboard.html):
    polls GET /health + raft status on all 3 nodes every 1s, drives the
    existing /chaos/* endpoints. Served by every node identically -- open
    it on any one of the three, it talks to all three via CORS (already
    wide open, see the middleware above). No real kill -9 button: see
    CLAUDE.md/README.md for why that was deliberately left unbuilt."""
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"))
