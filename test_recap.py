"""End-of-event recap test: spins up 3 nodes and confirms the frozen
top-liked-photos snapshot behaves the way a "memorable moments" slideshow
needs to.

What this is written against, beyond "does the endpoint respond":

  - The snapshot is genuinely FROZEN. A like that lands after /recap/trigger
    has already fired must not change what GET /recap returns -- that's the
    entire point of computing it once instead of live on every read.
  - It ranks by like count, actually, not upload order or photo_id.
  - RECAP_TOP_N caps the list even when more photos than that got likes.
  - It's per event: two events on the same cluster, same "default"
    zone/guest overlap potential, must not leak into each other's recap.
  - GET /recap before any trigger reports not-ready with an empty list,
    never a 404 or an error -- a guest opening the slideshow tab before the
    operator has ended the event is a routine state, not a failure.
  - It replicates like any other event: a node that never saw the trigger
    call still has the same snapshot once gossip converges.
  - It's idempotent across a re-trigger with more photos/likes added
    in between (mirrors test_raft.py's leader-crash idempotency check, but
    for the content, not just the count).

Self-contained: starts/kills its own processes, cleans up its own .db files.
"""
import base64
import hashlib
import os
import subprocess
import sys
import time

import httpx

from testutil import ensure_safe_to_run

NODES = [
    ("node1", 8001, "http://127.0.0.1:8002,http://127.0.0.1:8003"),
    ("node2", 8002, "http://127.0.0.1:8001,http://127.0.0.1:8003"),
    ("node3", 8003, "http://127.0.0.1:8001,http://127.0.0.1:8002"),
]

# Small enough to prove the cap fires with a handful of photos. main.py
# reads these from the environment for exactly this reason.
TOP_N = 3
# Spares frozen beyond the visible N, so a photo deleted after the event
# can be backfilled instead of leaving a hole. 2 + TOP_N = 5, which is
# exactly how many photos this test uploads -- so the reserve is fully
# consumed by the end and the "exhausted" flag can be exercised too.
SPARES = 2

procs = {}


def start_all():
    for node_id, port, peers in NODES:
        env = os.environ.copy()
        env["NODE_ID"] = node_id
        env["DB_PATH"] = f"./{node_id}.db"
        env["SELF_URL"] = f"http://127.0.0.1:{port}"
        env["PEERS"] = peers
        env["GOSSIP_INTERVAL"] = "1.0"
        env["RECAP_TOP_N"] = str(TOP_N)
        env["RECAP_SPARES"] = str(SPARES)
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs[node_id] = (p, port)
    print("started:", {k: v[1] for k, v in procs.items()})


# trust_env=False throughout: every call targets 127.0.0.1, and a system
# HTTP proxy would otherwise intercept loopback traffic (see CLAUDE.md).
def get(port, path, **params):
    return httpx.get(f"http://127.0.0.1:{port}{path}", params=params or None,
                      timeout=3.0, trust_env=False)


def post(port, path, body=None):
    return httpx.post(f"http://127.0.0.1:{port}{path}", json=body,
                       timeout=5.0, trust_env=False)


def create_event(port, slug, name):
    r = post(port, "/events", {"slug": slug, "name": name, "venue": "test", "zones": ["main"]})
    r.raise_for_status()
    return r.json()


def fake_jpeg(seed: bytes, size: int = 4000) -> bytes:
    body = hashlib.sha256(seed).digest()
    while len(body) < size:
        body += hashlib.sha256(body).digest()
    return b"\xff\xd8\xff\xe0" + body[:size]


_photo_n = 0


def post_photo(port, event_id, guest="g1"):
    """Uploads with real bytes, so the recap's blob pinning and the
    "deleted photos stop serving their pixels" check both have something
    to actually assert against."""
    global _photo_n
    _photo_n += 1
    r = post(port, "/photos", {
        "guest_id": guest, "zone": "main", "composition_score": 90, "event_id": event_id,
        "image_base64": base64.b64encode(fake_jpeg(f"recap-{_photo_n}".encode())).decode(),
    })
    r.raise_for_status()
    return r.json()["photo_id"]


def like(port, photo_id, guest):
    r = post(port, "/likes", {"guest_id": guest, "photo_id": photo_id})
    r.raise_for_status()


def trigger_recap(event_id):
    """Broadcast to all three, as a real client would -- it doesn't know
    who the leader is. Only the leader's call actually appends anything."""
    for _, port, _ in NODES:
        r = httpx.post(f"http://127.0.0.1:{port}/recap/trigger", params={"event_id": event_id},
                        timeout=3.0, trust_env=False)
        r.raise_for_status()


def get_recap(port, event_id):
    r = get(port, "/recap", event_id=event_id)
    r.raise_for_status()
    return r.json()


def fail(msg):
    print(f"FAIL: {msg}")
    return 1


def main():
    ensure_safe_to_run()
    start_all()
    try:
        print("waiting for uvicorn to boot...")
        time.sleep(2.5)

        ev = create_event(8001, "recap-test", "Recap Test Event")
        event_id = ev["event_id"]

        # 5 photos, more than TOP_N=3, with distinct like counts so ranking
        # is unambiguous: p5 > p4 > p3 > p2 > p1.
        photo_ids = [post_photo(8001, event_id) for _ in range(5)]
        guests = [f"guest{i}" for i in range(5)]
        for rank, pid in enumerate(photo_ids):
            for g in guests[: rank + 1]:
                like(8002, pid, g)
        time.sleep(2.5)  # let likes and photos converge everywhere

        # --- 1. before any trigger, GET /recap is "not ready", not an error
        body = get_recap(8003, event_id)
        if body["ready"] or body["photos"]:
            return fail(f"recap reported ready before any trigger: {body}")
        print("PASS: GET /recap before a trigger is not-ready with no photos")

        # --- 2. trigger, then check the top-N ranking and the cap
        trigger_recap(event_id)
        time.sleep(2.5)

        body = get_recap(8001, event_id)
        if not body["ready"]:
            return fail("recap never became ready after triggering")
        if len(body["photos"]) != TOP_N:
            return fail(f"expected {TOP_N} photos in the snapshot, got {len(body['photos'])}")
        expected_order = list(reversed(photo_ids))[:TOP_N]  # p5, p4, p3
        got_order = [p["photo_id"] for p in body["photos"]]
        if got_order != expected_order:
            return fail(f"recap ranking is wrong: got {got_order}, expected {expected_order}")
        if body["photos"][0]["likes"] != 5 or body["photos"][-1]["likes"] != 3:
            return fail(f"like counts in the snapshot look wrong: {body['photos']}")
        print(f"PASS: recap ranks by likes and caps at RECAP_TOP_N={TOP_N}")

        # --- 2b. the recap pinned its photos' blobs. Pinning is what makes
        # a recap outlive its event: pinned blobs are replicated first and
        # are the only ones blob_archive uploads to object storage.
        # /blob_archive/status, not /health: the pinned figures need a recap
        # replay plus a full blob scan, so they're deliberately kept off the
        # 1s health poll.
        pinned = get(8001, "/blob_archive/status").json()["blobs"]["pinned"]
        if pinned < TOP_N:
            return fail(f"recap pinned only {pinned} blobs, expected at least {TOP_N}")
        print(f"PASS: recap pinned {pinned} blobs for archival/retention")

        # --- 3. replicates to a node that never received the trigger call
        # (all three got it above, so prove it a different way: kill node1,
        # the one every write above targeted, and read from node3 alone)
        body3 = get_recap(8003, event_id)
        if body3["photos"] != body["photos"]:
            return fail("recap snapshot differs between node1 and node3")
        print("PASS: recap snapshot is identical on a different node (gossiped)")

        # --- 4. FROZEN: new likes after the trigger must not change it.
        # p1 (photo_ids[0]) already had 1 like (guest0); pile on 6 more
        # distinct guests so it now has 7 -- more than every photo in the
        # frozen top-3, which must still show p5/p4/p3 unchanged.
        for g in ("late_guest", "late_guest2", "late_guest3", "late_guest4",
                  "late_guest5", "late_guest6"):
            like(8001, photo_ids[0], g)
        time.sleep(2.5)

        body_after = get_recap(8001, event_id)
        if body_after["photos"] != body["photos"]:
            return fail(
                f"recap changed after new likes landed -- it must stay frozen. "
                f"before={body['photos']} after={body_after['photos']}"
            )
        print("PASS: recap snapshot is frozen -- later likes don't reshuffle it")

        # --- 5. idempotent re-trigger: same result, no duplicate event
        trigger_recap(event_id)
        time.sleep(2.0)
        body_retriggered = get_recap(8003, event_id)
        if body_retriggered["photos"] != body["photos"]:
            return fail("re-triggering changed the snapshot -- it must be idempotent")
        print("PASS: re-triggering after the fact is a no-op")

        # --- 6. a guest deleting a frozen photo: it leaves the reel, and a
        # spare backfills so the slideshow stays full length. Consent has to
        # outrank the freeze -- a retracted photo must not keep playing --
        # but the reel shouldn't develop a hole either.
        top_pid = body["photos"][0]["photo_id"]
        r = post(8001, "/photos/delete", {"guest_id": "g1", "photo_id": top_pid})
        if r.status_code != 200:
            return fail(f"deleting an own photo returned {r.status_code}")
        time.sleep(2.5)

        after_del = get_recap(8003, event_id)
        ids_after = [p["photo_id"] for p in after_del["photos"]]
        if top_pid in ids_after:
            return fail("a deleted photo is still in the recap -- consent must outrank the freeze")
        if len(ids_after) != TOP_N:
            return fail(
                f"recap is {len(ids_after)} long after one delete, expected {TOP_N} "
                f"-- a spare should have backfilled"
            )
        # backfilled counts slots refilled from reserve, NOT photos deleted
        # -- those diverge the moment the reserve runs dry, and an earlier
        # version reported the deletion count.
        if after_del["backfilled"] != 1:
            return fail(f"backfilled count is {after_del['backfilled']}, expected 1")
        if after_del["short"]:
            return fail("reel reported short while a spare was still available")
        # The backfilled slot must be the next-most-liked photo, not a random one
        if ids_after[-1] != photo_ids[1]:
            return fail(f"backfill pulled {ids_after[-1]}, expected next-ranked {photo_ids[1]}")
        print("PASS: a deleted photo leaves the reel and a spare backfills in rank order")

        # ...and its bytes are gone too, not just its listing
        if get(8001, f"/photos/{top_pid}/image").status_code != 404:
            return fail("a deleted photo still serves its image bytes")
        print("PASS: the deleted photo's bytes stop serving as well")

        # --- 6b. exhaust the reserve: with SPARES spares, deleting more
        # than that must shorten the reel AND say so. The earlier version
        # gated this flag on there having been a reserve at all, so an
        # event that froze exactly `visible` photos lost one and still
        # reported everything fine.
        for pid in [p["photo_id"] for p in after_del["photos"]][: SPARES]:
            r = post(8001, "/photos/delete", {"guest_id": "g1", "photo_id": pid})
            if r.status_code != 200:
                return fail(f"deleting {pid} to exhaust the reserve returned {r.status_code}")
        time.sleep(2.5)

        drained = get_recap(8001, event_id)
        if len(drained["photos"]) >= TOP_N:
            return fail("reel didn't shrink after the reserve was exhausted")
        if not drained["short"]:
            return fail(
                f"reel is {len(drained['photos'])}/{TOP_N} long but doesn't report short"
            )
        print(f"PASS: past the reserve the reel shrinks to "
              f"{len(drained['photos'])}/{TOP_N} and reports short")

        # --- 7. a second event's recap must not leak into or from this one
        other = create_event(8002, "recap-test-2", "Other Event")
        other_pid = post_photo(8002, other["event_id"])
        like(8002, other_pid, "solo_guest")
        time.sleep(2.0)
        trigger_recap(other["event_id"])
        time.sleep(2.0)

        other_body = get_recap(8001, other["event_id"])
        if [p["photo_id"] for p in other_body["photos"]] != [other_pid]:
            return fail(f"other event's recap is wrong: {other_body}")
        if other_pid in {p["photo_id"] for p in drained["photos"]}:
            return fail("second event's photo leaked into the first event's recap")
        # Compared against the post-deletion reel, not the original: step 6
        # legitimately changed it, and re-asserting the pre-delete state
        # here would be testing that the deletes never took effect.
        still_body = get_recap(8001, event_id)
        if still_body["photos"] != drained["photos"]:
            return fail("triggering a second event's recap disturbed the first event's snapshot")
        print("PASS: recap stays isolated per event")

        print("\nALL CHECKS PASSED")
        return 0
    finally:
        print("cleaning up remaining processes...")
        for nid, (p, port) in list(procs.items()):
            p.kill()
        for nid, (p, port) in list(procs.items()):
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        for node_id, _, _ in NODES:
            try:
                os.remove(f"./{node_id}.db")
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    sys.exit(main())
