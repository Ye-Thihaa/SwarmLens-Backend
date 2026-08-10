import asyncio
import base64
import os
import random
import re
import secrets
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import ai_engine
from store import DEFAULT_EVENT_ID, Store
from gossip import Gossip
from worker import Worker
from raft import Raft
from hashing import ring_for, owner_of
from cloud_sync import CloudSync, TRUST_ENV as CLOUD_SYNC_TRUST_ENV

QUORUM_EVENT_KINDS = ("photo", "like", "aesthetic_score", "photo_delete")
# How many of a single guest's own photos can sit in the cross-guest public
# gallery (GET /photos/public) at once -- a deliberate curation cap, not a
# technical one. Enforced server-side in POST /photos/public so it holds
# regardless of which node a guest's toggle happens to land on.
# Env-overridable so test_events.py can drive the cap to its edge in a few
# requests instead of uploading 26 photos twice. The default is the real
# number and is mirrored by hand in client-2/src/lib/api.ts.
PUBLIC_LIMIT_PER_GUEST = int(os.getenv("PUBLIC_LIMIT_PER_GUEST", "25"))

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
# Gates the chaos endpoints (see require_operator_token below). Fails
# open -- same as always -- when unset, so test_quorum.py, load_test.py
# and dashboard.html, none of which ever set this, keep working exactly
# as before. Set it to actually protect a real running cluster; the
# operator console (client-2) sends it as X-Operator-Token once its own
# password gate (a separate secret, see client-2/.env) has been passed.
OPERATOR_TOKEN = os.getenv("OPERATOR_TOKEN", "")

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


def ring_key(zone: str, event_id: str | None) -> str:
    """What the consistent-hash ring actually hashes for a zone.

    Two events hosted on the same cluster will both have a zone called
    "main" (or "entrance", or "dance_floor") -- distinct corners of
    distinct rooms that happen to share a label. Hashing the bare zone
    name maps both to one owning node and, worse, makes /zones proxy the
    wedding's "main" score from a node computing the donation's "main",
    so the two events silently share one number. Namespacing by event is
    what keeps them separate on the ring.

    event_id=None keeps the pre-multi-event key exactly as it was, so a
    single-event cluster's ring assignment doesn't move (and neither do
    test_hashing.py's expectations)."""
    return zone if event_id is None else f"{event_id}/{zone}"


async def _zone_scores_local(event_id: str | None = None) -> dict[str, dict]:
    """Derived read: photos + CRDT like counts (store.photos()) plus the
    average aesthetic_score per zone (store.aesthetic_scores()) for
    whichever photos have been through /analyze so far -- None for a zone
    with no scored photos yet, not 0, so it's distinguishable from 'scored
    and bad'. Scoped to one event when event_id is given; None merges
    every event, which is what callers predating multi-event hosting
    still ask for."""
    aesthetic = await store.aesthetic_scores()
    zones: dict[str, dict] = {}
    scored: dict[str, list] = {}
    for p in await store.photos(event_id):
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


def _zone_scores_from_events(events, event_id: str | None = None) -> dict[str, dict]:
    """Same derivation as _zone_scores_local, but over an arbitrary merged
    event set instead of this node's own DB -- what /zones/quorum uses to
    recompute from the union of several nodes' partial views.

    Event filtering happens here, after the merge, rather than on the wire
    in /zones/events. A node can hold a like whose photo it hasn't
    received yet; dropping that like at the source because the sending
    node can't tell which event it belongs to would lose a count the
    merged view could have resolved. Only the photo carries an event_id,
    so restricting photo_zone below is enough -- likes, scores and
    tombstones for photos outside this event are simply never looked
    up."""
    photo_zone: dict[str, str] = {}
    like_guests: dict[str, set] = {}
    aesthetic: dict[str, float] = {}
    deleted: set[str] = set()
    for e in sorted(events, key=lambda e: e["created_at"]):
        p = e["payload"]
        if e["kind"] == "photo":
            if event_id is not None and p.get("event_id", DEFAULT_EVENT_ID) != event_id:
                continue
            photo_zone[p["photo_id"]] = p["zone"]
        elif e["kind"] == "like":
            like_guests.setdefault(p["photo_id"], set()).add(p["guest_id"])
        elif e["kind"] == "aesthetic_score":
            aesthetic[p["photo_id"]] = p["score"]  # last write wins -- sorted by created_at above
        elif e["kind"] == "photo_delete":
            deleted.add(p["photo_id"])

    zones: dict[str, dict] = {}
    scored: dict[str, list] = {}
    for photo_id, zone in photo_zone.items():
        if photo_id in deleted:
            continue
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


async def require_operator_token(x_operator_token: str = Header(default="")):
    """Gate for the chaos and event-management endpoints.
    secrets.compare_digest avoids a timing side-channel that would
    otherwise let an attacker guess the token one byte at a time. No-op
    (open) when OPERATOR_TOKEN isn't configured on this node -- see the
    constant's own comment for why."""
    if not OPERATOR_TOKEN:
        return
    if not secrets.compare_digest(x_operator_token, OPERATOR_TOKEN):
        raise HTTPException(401, "invalid or missing X-Operator-Token")


# ---------------------------- hosted events ----------------------------

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")


class EventIn(BaseModel):
    slug: str
    name: str
    venue: str = ""
    when: str = ""
    # The corners of this room a guest may tag a frame with. One zone (or
    # none, which becomes ["main"]) means the guest app shows no zone
    # picker at all -- most events are a single location and asking
    # someone to declare where they're standing is a question they
    # shouldn't have to answer. Several means the host has genuinely
    # divided the day (ceremony / reception), and the picker returns.
    zones: list[str] = []
    # Blank on create -> the server mints both. Passing an existing
    # event_id back is how an edit works: another event_created append,
    # last write wins (store.events_catalog).
    event_id: str = ""
    join_token: str = ""


@app.post("/events")
async def create_event(body: EventIn, _: None = Depends(require_operator_token)):
    """Register (or edit) a hosted event. Appends an event_created event
    -- gossiped, merged and replayed exactly like a photo, no new table
    and no new distributed mechanism.

    Slug uniqueness is checked against this node's replica only, and that
    is genuinely all it can be: two operators creating the same slug on
    two nodes during a partition will both succeed, and no amount of
    checking here changes that. What makes it safe is that resolution is
    deterministic -- GET /events/{slug} below picks the earliest-created
    match, which every node computes identically once gossip converges,
    so the losing event keeps its own event_id and its photos rather than
    being silently merged into the winner's."""
    slug = body.slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(400, "slug must be lowercase letters, digits and dashes")
    if not body.name.strip():
        raise HTTPException(400, "an event needs a name")

    catalog = await store.events_catalog()
    event_id = body.event_id or f"ev_{uuid.uuid4().hex[:8]}"
    clash = next(
        (e for e in catalog.values() if e["slug"] == slug and e["event_id"] != event_id), None
    )
    if clash:
        raise HTTPException(409, f"slug '{slug}' already belongs to {clash['event_id']}")

    existing = catalog.get(event_id)
    payload = {
        "event_id": event_id,
        "slug": slug,
        "name": body.name.strip(),
        "venue": body.venue.strip(),
        "when": body.when.strip(),
        "zones": [z.strip() for z in body.zones if z.strip()] or ["main"],
        # Kept across edits: rotating it on every rename would silently
        # invalidate every QR already printed and sitting on the tables.
        "join_token": body.join_token
        or (existing or {}).get("join_token")
        or secrets.token_urlsafe(8),
        "created_by": NODE_ID,
    }
    await store.append_local("event_created", payload)
    return {"ok": True, **payload}


@app.get("/events")
async def list_events(_: None = Depends(require_operator_token)):
    """Every event this node knows about, join tokens included -- the
    operator console's list. Gated because a public directory of every
    event on the cluster is the thing multi-tenant hosting is supposed to
    prevent: a wedding's guests have no business enumerating the funeral
    booked the same weekend. Guests resolve exactly one event, by slug
    plus token, through the endpoint below."""
    catalog = await store.events_catalog()
    return {
        "node": NODE_ID,
        "events": sorted(catalog.values(), key=lambda e: e["slug"]),
    }


@app.get("/events/{slug}")
async def resolve_event(slug: str, k: str = ""):
    """What a scanned QR lands on: slug -> the event_id every subsequent
    guest call carries.

    `k` is NOT authentication and isn't presented as any: anyone holding
    the URL is in, exactly like GET /photos/public. It stops a guest who
    guesses or mistypes a slug from wandering into someone else's event,
    which is a real failure mode at a venue hosting three parties in one
    weekend -- nothing more. The token is never echoed back, so a
    successful resolve doesn't hand out a shareable credential the
    scanner didn't already have.

    Earliest-created match wins when two events somehow share a slug --
    see POST /events for why that's the honest resolution rule."""
    matches = [e for e in (await store.events_catalog()).values() if e["slug"] == slug.lower()]
    if not matches:
        raise HTTPException(404, "no such event")
    # event_id breaks a created_at tie so every node picks the same winner
    # even when two clocks read identically.
    event = min(matches, key=lambda e: (e["created_at"], e["event_id"]))
    if event.get("join_token") and not secrets.compare_digest(k, event["join_token"]):
        raise HTTPException(404, "no such event")
    return {"node": NODE_ID, "event": {kk: v for kk, v in event.items() if kk != "join_token"}}


# ---------------------------- client API ----------------------------

class PhotoIn(BaseModel):
    guest_id: str
    zone: str
    url: str = ""
    composition_score: int = 0
    vclock: dict[str, int] = {}
    image_base64: str | None = None
    # Which hosted event this frame belongs to. Deliberately the ONLY
    # request model that carries one: a like, an aesthetic score, a public
    # mark and a delete tombstone all name a globally-unique photo_id and
    # inherit that photo's event, so event membership is recorded in
    # exactly one place. Putting it on all five would create four more
    # copies that can disagree with the photo -- and a client that got one
    # wrong could push a like into an event its photo isn't in.
    event_id: str = DEFAULT_EVENT_ID


class LikeIn(BaseModel):
    guest_id: str
    photo_id: str
    vclock: dict[str, int] = {}


class PublicMarkIn(BaseModel):
    guest_id: str
    photo_id: str
    public: bool
    vclock: dict[str, int] = {}


class PhotoDeleteIn(BaseModel):
    guest_id: str
    photo_id: str
    vclock: dict[str, int] = {}


@app.post("/photos")
async def upload_photo(body: PhotoIn):
    """Accepts any event_id without checking it exists. That looks lax and
    isn't: event_created reaches this node by gossip like everything else,
    so a guest who scans a QR and shoots within the first second can
    legitimately hit a node that hasn't learned the event yet. Rejecting
    would turn a normal replication lag into a lost photo -- exactly the
    failure this whole system is built to avoid. An unknown event_id costs
    nothing: the photo is simply invisible until a matching event exists,
    and visible the moment one does."""
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
async def list_photos(event_id: str | None = None):
    """Gallery as this node currently sees it. Each photo carries its own
    vclock (empty if the guest client didn't attach one) plus
    concurrent_with: other photo_ids whose vclock is concurrent with this
    one -- neither happened before the other -- so a gallery UI can
    render them side by side instead of implying a false total order
    from arrival time. O(n^2) over this node's photos; fine at demo
    scale, same tradeoff /zones already makes.

    event_id scopes this to one hosted event. concurrent_with is computed
    *after* that filter on purpose: telling a wedding guest their frame
    was shot at the same moment as one from a donation ceremony they'll
    never see is noise, not causality they can act on."""
    photos = await store.photos(event_id)
    for p in photos:
        p["concurrent_with"] = [
            q["photo_id"] for q in photos
            if q["photo_id"] != p["photo_id"] and _concurrent(p["vclock"], q["vclock"])
        ]
    return {"node": NODE_ID, "photos": photos}


@app.get("/photos/{photo_id}/image")
async def photo_image(photo_id: str):
    """Serves one photo's actual image bytes, decoded from the same event
    log entry gossip already replicated to every node -- no separate media
    storage or CDN, this repo has none (see ai_engine.py's /analyze
    docstring). Split out from GET /photos so the list endpoint (polled
    every few seconds by the client) stays light instead of shipping every
    photo's full bytes on every poll. 404 if this photo has no
    image_base64 attached (metadata-only test photos, or captures from a
    guest client older than this endpoint)."""
    b64 = await store.photo_image_b64(photo_id)
    if not b64:
        raise HTTPException(404, "no image stored for this photo")
    try:
        image_bytes = base64.b64decode(b64, validate=True)
    except Exception:
        raise HTTPException(500, "corrupt stored image")
    return Response(content=image_bytes, media_type="image/jpeg")


@app.post("/photos/public")
async def mark_photo_public(body: PublicMarkIn):
    """Opts one of THIS guest's own photos into (or out of) the
    cross-guest public gallery (GET /photos/public below) -- individual
    photos a guest hand-picks, not the composited strips routes/album.tsx
    already posts to zone "photo_booth". Ownership is checked against the
    photo event's own guest_id, not the caller-supplied one, so the
    per-guest cap below can't be bypassed by mislabeling whose photo it
    is. Appends a public_mark event -- gossips, replicates and converges
    exactly like every other event kind, no new distributed mechanism."""
    photo = await store.photo_by_id(body.photo_id)
    if not photo:
        raise HTTPException(404, "unknown photo")
    if photo["guest_id"] != body.guest_id:
        raise HTTPException(403, "only a photo's own guest can change its public status")
    if body.public:
        state = await store.public_state()
        already_public = state.get(body.photo_id, {}).get("public", False)
        if not already_public:
            # The cap is per guest PER EVENT, and the event comes from the
            # photo's own record, not from anything the caller sent. A
            # guest who shoots at three weddings gets 25 public frames at
            # each -- counting across all of them would silently spend
            # tonight's quota on last month's.
            event_id = photo.get("event_id", DEFAULT_EVENT_ID)
            in_event = {p["photo_id"] for p in await store.photos(event_id)}
            count = sum(
                1
                for pid, v in state.items()
                if v["public"] and v["guest_id"] == body.guest_id and pid in in_event
            )
            if count >= PUBLIC_LIMIT_PER_GUEST:
                raise HTTPException(
                    409, f"already at the {PUBLIC_LIMIT_PER_GUEST}-photo public limit for this event"
                )
    await store.append_local("public_mark", body.model_dump(exclude={"vclock"}), vclock=body.vclock)
    return {"ok": True, "public": body.public}


@app.get("/photos/public")
async def public_photos(event_id: str | None = None):
    """The cross-guest public gallery: every photo whose own guest has
    opted it in via POST /photos/public above, latest toggle wins --
    derived from the log the same way every other read here is, no
    second "is_public" column living outside the event stream. GET
    /photos already carries everything a gallery card needs (guest_id,
    likes, taken_at, zone); this just filters that down to the public
    subset instead of introducing a parallel shape."""
    state = await store.public_state()
    photos = await store.photos(event_id)
    public = [p for p in photos if state.get(p["photo_id"], {}).get("public")]
    return {"node": NODE_ID, "photos": public}


@app.post("/photos/delete")
async def delete_photo(body: PhotoDeleteIn):
    """Retracts one of THIS guest's own photos everywhere: the room feed
    (GET /photos), zone scores (local and quorum), likes, and the public
    gallery, on every node once gossip catches up. A photo that was never
    made public doesn't come through here at all -- client-2's mine.tsx
    only calls this endpoint for a photo currently in the public gallery;
    a private roll photo is deleted by the guest's own device splicing it
    out of localStorage, since the event log never replicated it anywhere
    that needs telling. Appends photo_delete, a tombstone every derived
    read filters on (store.deleted_photo_ids) -- one choke point, same
    pattern as public_mark, not a second flag column. Ownership is
    checked against the photo event's own guest_id, never the
    caller-supplied one. Idempotent: deleting an already-deleted photo_id
    just appends a second, harmless tombstone."""
    photo = await store.photo_by_id(body.photo_id)
    if not photo:
        raise HTTPException(404, "unknown photo")
    if photo["guest_id"] != body.guest_id:
        raise HTTPException(403, "only a photo's own guest can delete it")
    await store.append_local("photo_delete", body.model_dump(exclude={"vclock"}), vclock=body.vclock)
    return {"ok": True}


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


class AnalyzePreviewIn(BaseModel):
    image_base64: str
    # The crop ratio the client is actually shooting (width/height), so the
    # rectangle it draws matches the frame it will get. Optional -- falls
    # back to the submitted frame's own aspect.
    aspect: float | None = None


# How many preview analyses may run at once on this node. Each one occupies
# a thread for ~0.05-1s of ONNX/OpenCV work; several guests pointing their
# viewfinders at the same node would otherwise saturate the default thread
# pool, which is shared with every other asyncio.to_thread caller here.
PREVIEW_CONCURRENCY = int(os.getenv("PREVIEW_CONCURRENCY", "2"))
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
_preview_slots = asyncio.Semaphore(PREVIEW_CONCURRENCY)


@app.post("/analyze/preview")
async def analyze_preview(body: AnalyzePreviewIn):
    """Live-viewfinder guidance: subject box, a rule-of-thirds crop
    rectangle, a film-stock recommendation and why. Same models as
    /analyze (ai_engine.preview) and the same to_thread offload.

    The difference that matters is that this writes NOTHING. /analyze
    appends an aesthetic_score event, which is correct for a photo that
    exists; a viewfinder calls this every couple of seconds against a
    frame that was never shot and has no photo_id. Persisting those would
    flood the append-only log, gossip every one of them to all three
    nodes, and drag /zones' avg_aesthetic toward frames no guest ever
    kept. If you add a field here, resist the urge to record it.

    Fails fast with 503 when every preview slot is busy rather than
    queueing: guidance computed for a frame the guest has already panned
    away from is worse than no guidance, and a queue would keep serving
    staler and staler answers under load. The client simply asks again on
    its next tick."""
    if len(body.image_base64) > MAX_PREVIEW_BYTES:
        raise HTTPException(413, "preview frame too large -- downscale before sending")
    if body.aspect is not None and not (0.2 <= body.aspect <= 5.0):
        raise HTTPException(400, "aspect must be between 0.2 and 5.0")
    try:
        image_bytes = base64.b64decode(body.image_base64, validate=True)
    except Exception:
        raise HTTPException(400, "image_base64 is not valid base64")

    if _preview_slots.locked():
        raise HTTPException(503, "preview busy, try the next frame")

    async with _preview_slots:
        try:
            return await asyncio.to_thread(ai_engine.preview, image_bytes, body.aspect)
        except Exception as e:
            # A malformed/undecodable frame is a client problem, not a node
            # fault -- and this endpoint is called on a loop, so a 500 here
            # would spam the log a few times a second.
            raise HTTPException(400, f"could not analyze frame: {e}")


@app.get("/zones")
async def zone_scores(event_id: str | None = None):
    """The emergent aesthetic map. Each zone's authoritative score comes
    from whichever node owns it on the consistent-hash ring -- proxied
    live if that's a peer, computed directly if it's us. If the owner is
    unreachable, falls back to this node's own (possibly lagging) replica
    with stale: true rather than failing the request.

    Ownership is per (event, zone) -- see ring_key. The proxied call
    carries event_id through, so the owner recomputes the same event's
    score rather than answering with its merged view of every event it
    happens to hold."""
    local = await _zone_scores_local(event_id)
    ring = ring_for(cluster_members())

    out = []
    # trust_env=False: `owner` is always a peer from PEERS/SELF_ID, i.e.
    # always 127.0.0.1 -- see the same note in gossip.py for why a system
    # proxy would otherwise break this.
    async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
        for zone, mine in local.items():
            owner = owner_of(ring_key(zone, event_id), ring)
            if owner == SELF_ID:
                out.append({**mine, "owner": owner, "stale": False})
                continue
            try:
                params = {"event_id": event_id} if event_id is not None else None
                r = await client.get(f"{owner}/zones/local", params=params)
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
async def zones_local(event_id: str | None = None):
    """This node's own replica, every zone, no ownership filtering. Not
    for clients -- this is what peers call when proxying a zone they
    don't own, and what a node falls back to reading from itself when the
    owner is unreachable."""
    scores = await _zone_scores_local(event_id)
    return {"node": NODE_ID, "zones": _rank(list(scores.values()))}


@app.get("/zones/ring")
async def zones_ring(event_id: str | None = None):
    """Debug: current membership and the zone -> owner mapping it implies.
    Useful for demoing how few zones move when a node joins or leaves --
    and, with event_id, for showing that two events' identically-named
    zones land on different owners rather than colliding."""
    members = cluster_members()
    ring = ring_for(members)
    scores = await _zone_scores_local(event_id)
    owners = {zone: owner_of(ring_key(zone, event_id), ring) for zone in scores}
    return {"node": NODE_ID, "event_id": event_id, "members": members, "owners": owners}


@app.get("/zones/events")
async def zones_events():
    """Raw photo/like/aesthetic_score events from this node's own replica,
    undigested. Not for clients -- this is what /zones/quorum fetches from
    each node it samples, so it can union events across nodes and
    recompute rather than trust one node's already-aggregated counts."""
    return {"node": NODE_ID, "events": await store.raw_events(QUORUM_EVENT_KINDS)}


@app.get("/zones/quorum")
async def zones_quorum(R: int = 2, W: int = 2, event_id: str | None = None):
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

    scores = _zone_scores_from_events(merged.values(), event_id)
    return {
        "node": NODE_ID,
        "event_id": event_id,
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
async def partition(peer_index: int, _: None = Depends(require_operator_token)):
    """Simulate a network split without touching Docker or iptables."""
    peer = PEERS[peer_index]
    gossip.partitioned.add(peer)
    return {"partitioned_from": peer}


@app.post("/chaos/heal")
async def heal(_: None = Depends(require_operator_token)):
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
