"""Multi-event hosting test: spins up 3 nodes and confirms that two events
hosted on the SAME cluster and the same event log stay separate everywhere a
guest can see.

The interesting failures this is written against are not "does the filter
work" but the ones that look fine until two events exist:

  - Both events name a zone "main". Hashing the bare zone name would put
    them on one ring position, so /zones for the wedding would be proxied
    from a node computing the donation's score. Ownership must be keyed by
    (event, zone).
  - The per-guest public-gallery cap counted every photo a guest had ever
    made public. A guest at their second event would arrive with the quota
    already spent.
  - Photos logged before any of this existed carry no event_id at all and
    must not become invisible.
  - A quorum read unions raw events from several nodes; filtering the wrong
    side of that merge drops likes whose photo only the other node held.

Self-contained: starts/kills its own processes, cleans up its own .db files.
"""
import os
import subprocess
import sys
import time

import httpx

from hashing import owner_of, ring_for
from testutil import ensure_safe_to_run

NODES = [
    ("node1", 8001, "http://127.0.0.1:8002,http://127.0.0.1:8003"),
    ("node2", 8002, "http://127.0.0.1:8001,http://127.0.0.1:8003"),
    ("node3", 8003, "http://127.0.0.1:8001,http://127.0.0.1:8002"),
]

# Small enough to hit in a couple of requests. main.py reads this from the
# environment for exactly this reason; the real default is 25.
PUBLIC_LIMIT = 2

procs = {}


def start_all():
    for node_id, port, peers in NODES:
        env = os.environ.copy()
        env["NODE_ID"] = node_id
        env["DB_PATH"] = f"./{node_id}.db"
        env["SELF_URL"] = f"http://127.0.0.1:{port}"
        env["PEERS"] = peers
        env["GOSSIP_INTERVAL"] = "1.0"
        env["PUBLIC_LIMIT_PER_GUEST"] = str(PUBLIC_LIMIT)
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
    r = httpx.get(f"http://127.0.0.1:{port}{path}", params=params or None,
                  timeout=3.0, trust_env=False)
    return r


def post(port, path, body=None):
    return httpx.post(f"http://127.0.0.1:{port}{path}", json=body,
                      timeout=5.0, trust_env=False)


def create_event(port, slug, name, zones):
    r = post(port, "/events", {"slug": slug, "name": name, "venue": "test", "zones": zones})
    r.raise_for_status()
    return r.json()


def post_photo(port, event_id, zone, guest="g1"):
    r = post(port, "/photos", {
        "guest_id": guest, "zone": zone, "composition_score": 90, "event_id": event_id,
    })
    r.raise_for_status()
    return r.json()["photo_id"]


def photos(port, event_id=None):
    r = get(port, "/photos", **({"event_id": event_id} if event_id else {}))
    r.raise_for_status()
    return r.json()["photos"]


def zones(port, event_id=None):
    r = get(port, "/zones", **({"event_id": event_id} if event_id else {}))
    r.raise_for_status()
    return {z["zone"]: z for z in r.json()["zones"]}


def fail(msg):
    print(f"FAIL: {msg}")
    return 1


def main():
    ensure_safe_to_run()
    start_all()
    try:
        print("waiting for uvicorn to boot...")
        time.sleep(2.5)

        # --- legacy photo: no event_id at all, posted before any event exists
        r = post(8001, "/photos", {"guest_id": "old", "zone": "main", "composition_score": 50})
        r.raise_for_status()
        legacy_id = r.json()["photo_id"]

        # --- two events, created on two DIFFERENT nodes, both with a "main" zone
        wedding = create_event(8001, "hollis-marchetti", "Hollis x Marchetti", ["main", "dance_floor"])
        donation = create_event(8002, "shwedagon-alu", "Shwedagon Alu", ["main"])
        print(f"created {wedding['event_id']} (wedding) on node1, "
              f"{donation['event_id']} (donation) on node2")

        if not wedding["join_token"] or wedding["join_token"] == donation["join_token"]:
            return fail("join tokens are missing or not unique per event")

        time.sleep(3.0)  # gossip both event_created events everywhere

        # --- 1. the catalog replicates like any other event
        r = get(8003, "/events")
        r.raise_for_status()
        seen = {e["event_id"] for e in r.json()["events"]}
        if wedding["event_id"] not in seen or donation["event_id"] not in seen:
            return fail(f"node3 never learned both events by gossip, saw {seen}")
        print("PASS: both events replicated to a node that created neither")

        # --- 2. slug is claimed cluster-wide (best-effort, see main.py)
        if post(8003, "/events", {"slug": "hollis-marchetti", "name": "Impostor"}).status_code != 409:
            return fail("a duplicate slug was accepted on a node that only learned it by gossip")
        print("PASS: duplicate slug rejected on a third node")

        # --- 3. QR resolution: slug + token in, event_id out, token never echoed
        r = get(8003, f"/events/{wedding['slug']}", k=wedding["join_token"])
        if r.status_code != 200:
            return fail(f"resolving a valid slug+token returned {r.status_code}")
        resolved = r.json()["event"]
        if resolved["event_id"] != wedding["event_id"]:
            return fail("slug resolved to the wrong event")
        if "join_token" in resolved:
            return fail("resolve handed back the join token -- it must not be re-shareable")
        if get(8003, f"/events/{wedding['slug']}", k=donation["join_token"]).status_code != 404:
            return fail("another event's token opened this event")
        if get(8003, f"/events/{wedding['slug']}").status_code != 404:
            return fail("a tokenless resolve succeeded")
        print("PASS: slug+token resolves, wrong/absent token 404s, token not echoed")

        # --- 4. photos: same zone name, same guest, two events
        w1 = post_photo(8001, wedding["event_id"], "main")
        w2 = post_photo(8001, wedding["event_id"], "main")
        post_photo(8002, wedding["event_id"], "dance_floor")
        d1 = post_photo(8002, donation["event_id"], "main")
        time.sleep(3.0)

        for port in (8001, 8002, 8003):
            w_ids = {p["photo_id"] for p in photos(port, wedding["event_id"])}
            d_ids = {p["photo_id"] for p in photos(port, donation["event_id"])}
            if w_ids & d_ids:
                return fail(f"node on :{port} leaked photos between events")
            if not {w1, w2} <= w_ids:
                return fail(f"node on :{port} is missing wedding photos: {w_ids}")
            if d_ids != {d1}:
                return fail(f"node on :{port} sees the wrong donation photos: {d_ids}")
            if legacy_id in w_ids or legacy_id in d_ids:
                return fail(f"the pre-event_id photo leaked into a real event on :{port}")
        print("PASS: every node scopes /photos to one event, on all three replicas")

        # --- 5. the legacy photo is still reachable, not orphaned
        if legacy_id not in {p["photo_id"] for p in photos(8003, "default")}:
            return fail("a photo logged with no event_id is unreachable")
        if legacy_id not in {p["photo_id"] for p in photos(8003)}:
            return fail("the unfiltered /photos no longer returns everything")
        print("PASS: photos predating event_id land in 'default' and stay visible")

        # --- 6. zone scores don't merge across events despite the shared name
        wz, dz = zones(8001, wedding["event_id"]), zones(8001, donation["event_id"])
        if wz["main"]["photos"] != 2:
            return fail(f"wedding 'main' counted {wz['main']['photos']} photos, expected 2")
        if dz["main"]["photos"] != 1:
            return fail(f"donation 'main' counted {dz['main']['photos']} photos, expected 1")
        if "dance_floor" in dz:
            return fail("a zone from the wedding appeared in the donation's map")
        print("PASS: identically-named zones score independently per event")

        # --- 7. ...and land on independent ring positions, not one shared owner
        members = sorted(f"http://127.0.0.1:{p}" for _, p, _ in NODES)
        ring = ring_for(members)
        spread = {owner_of(f"ev_{i:02x}/main", ring) for i in range(30)}
        if len(spread) < 2:
            return fail("namespaced zone keys all hash to one node -- ring key isn't event-scoped")
        print(f"PASS: 'main' across 30 events spreads over {len(spread)} owners, not 1")

        # --- 8. quorum reads filter after the merge, on every R
        for R in (1, 2, 3):
            r = get(8001, "/zones/quorum", R=R, event_id=donation["event_id"])
            r.raise_for_status()
            body = r.json()
            if body["event_id"] != donation["event_id"]:
                return fail("quorum read didn't echo the event it was scoped to")
            total = sum(z["photos"] for z in body["zones"])
            if total != 1:
                return fail(f"quorum R={R} counted {total} donation photos, expected 1")
        print("PASS: quorum reads stay event-scoped at R=1, 2 and 3")

        # --- 9. the public-gallery cap is per guest PER EVENT
        for pid in (w1, w2):
            r = post(8001, "/photos/public", {"guest_id": "g1", "photo_id": pid, "public": True})
            if r.status_code != 200:
                return fail(f"publishing wedding photo {pid} returned {r.status_code}")
        w3 = post_photo(8001, wedding["event_id"], "main")
        r = post(8001, "/photos/public", {"guest_id": "g1", "photo_id": w3, "public": True})
        if r.status_code != 409:
            return fail(f"the {PUBLIC_LIMIT}-photo cap didn't fire, got {r.status_code}")
        # Same guest, other event: the quota must be untouched.
        r = post(8001, "/photos/public", {"guest_id": "g1", "photo_id": d1, "public": True})
        if r.status_code != 200:
            return fail(
                f"the same guest was blocked at {r.status_code} in a DIFFERENT event -- "
                "the cap is counting across events"
            )
        print(f"PASS: cap of {PUBLIC_LIMIT} fires within an event and resets in the next one")

        # --- 10. and the public gallery itself is scoped
        time.sleep(2.5)
        r = get(8003, "/photos/public", event_id=donation["event_id"])
        r.raise_for_status()
        pub = {p["photo_id"] for p in r.json()["photos"]}
        if pub != {d1}:
            return fail(f"donation's public gallery shows {pub}, expected just the one photo")
        print("PASS: /photos/public is event-scoped and converged on a third node")

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
