import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from store import Store
from gossip import Gossip
from worker import Worker

NODE_ID = os.getenv("NODE_ID", "node1")
DB_PATH = os.getenv("DB_PATH", f"./{NODE_ID}.db")
PEERS = [p for p in os.getenv("PEERS", "").split(",") if p]
INTERVAL = float(os.getenv("GOSSIP_INTERVAL", "1.0"))

store = Store(DB_PATH, NODE_ID)
gossip = Gossip(NODE_ID, PEERS, store, INTERVAL)
worker = Worker(NODE_ID, store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.open()
    await gossip.start()
    await worker.start()
    yield
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
    """The emergent aesthetic map: which zones are earning the most attention."""
    scores: dict[str, dict] = {}
    for p in await store.photos():
        z = scores.setdefault(p["zone"], {"zone": p["zone"], "photos": 0, "likes": 0})
        z["photos"] += 1
        z["likes"] += p["likes"]
    ranked = sorted(scores.values(), key=lambda z: -(z["likes"] * 2 + z["photos"]))
    return {"node": NODE_ID, "zones": ranked}


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
