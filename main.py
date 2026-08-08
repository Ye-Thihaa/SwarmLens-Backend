import os
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from store import Store
from gossip import Gossip
from worker import Worker
from raft import Raft
from hashing import ring_for, owner_of

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


def cluster_members() -> list[str]:
    """Currently alive nodes (self + whatever gossip's failure detector
    still considers reachable). Recomputed fresh on every call, so the
    ring always reflects current membership -- nothing cached to go stale.
    Self is identified by SELF_ID (its URL) to match how peers list it,
    not NODE_ID -- see the SELF_URL comment above."""
    return sorted({SELF_ID, *gossip.alive_peers()})


async def _zone_scores_local() -> dict[str, dict]:
    scores: dict[str, dict] = {}
    for p in await store.photos():
        z = scores.setdefault(p["zone"], {"zone": p["zone"], "photos": 0, "likes": 0})
        z["photos"] += 1
        z["likes"] += p["likes"]
    return scores


def _rank(zones) -> list[dict]:
    return sorted(zones, key=lambda z: -(z["likes"] * 2 + z["photos"]))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.open()
    await gossip.start()
    await worker.start()
    await raft.start()
    yield
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


class LikeIn(BaseModel):
    guest_id: str
    photo_id: str


@app.post("/photos")
async def upload_photo(body: PhotoIn):
    payload = body.model_dump()
    payload["photo_id"] = f"ph_{uuid.uuid4().hex[:8]}"
    event = await store.append_local("photo", payload)
    return {"ok": True, "photo_id": payload["photo_id"], "seq": event["seq"]}


@app.post("/likes")
async def like_photo(body: LikeIn):
    await store.append_local("like", body.model_dump())
    return {"ok": True, "likes": await store.like_count(body.photo_id)}


@app.get("/photos")
async def list_photos():
    return {"node": NODE_ID, "photos": await store.photos()}


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
    async with httpx.AsyncClient(timeout=1.0) as client:
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
