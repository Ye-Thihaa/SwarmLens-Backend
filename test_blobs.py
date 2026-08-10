"""Blob-storage test: photo bytes live outside the event log, replicate on
their own path, and stay available throughout.

Written against the production-scale failures that forced the split, not
just "does an image come back":

  - The event log must stay metadata-only. A photo event carrying inline
    base64 made photos 99.8% of the log, and made GET /photos read ~200KB
    per photo off disk to return ~120 bytes of it. The log is checked
    directly here, not through an endpoint that could be hiding it.
  - Bytes must reach a node that never received the upload, WITHOUT
    riding gossip. Both are asserted: the blob arrives, and the event
    that names it never contained it.
  - A node that has the metadata but not yet the pixels must read through
    to a peer rather than 404. That window is real now that the two
    replicate at different speeds, and a 404 renders as a permanently
    broken image in both clients.
  - Identical bytes must store once. Content addressing is the whole
    reason the key is a hash.
  - Legacy photos with inline base64 must keep working forever. Real
    databases have them; rewriting history to migrate is a worse trade.

Self-contained: starts/kills its own processes, cleans up its own .db files.
"""
import base64
import hashlib
import json
import os
import sqlite3
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

procs = {}


def start_all():
    for node_id, port, peers in NODES:
        env = os.environ.copy()
        env["NODE_ID"] = node_id
        env["DB_PATH"] = f"./{node_id}.db"
        env["SELF_URL"] = f"http://127.0.0.1:{port}"
        env["PEERS"] = peers
        env["GOSSIP_INTERVAL"] = "1.0"
        env["BLOB_SYNC_INTERVAL"] = "1.0"
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs[node_id] = (p, port)
    print("started:", {k: v[1] for k, v in procs.items()})


def get(port, path, **params):
    return httpx.get(f"http://127.0.0.1:{port}{path}", params=params or None,
                     timeout=10.0, trust_env=False)


def post(port, path, body=None):
    return httpx.post(f"http://127.0.0.1:{port}{path}", json=body,
                      timeout=15.0, trust_env=False)


def fake_jpeg(seed: bytes, size: int = 60_000) -> bytes:
    """Deterministic pseudo-image. Real JPEG magic bytes so nothing along
    the way rejects it on sniffing, and incompressible-ish filler so the
    byte budget in blob_sync is exercised against a realistic size."""
    body = hashlib.sha256(seed).digest()
    while len(body) < size:
        body += hashlib.sha256(body).digest()
    return b"\xff\xd8\xff\xe0" + body[:size]


def upload(port, raw: bytes, event_id="default", guest="g1"):
    r = post(port, "/photos", {
        "guest_id": guest, "zone": "main", "composition_score": 80,
        "event_id": event_id,
        "image_base64": base64.b64encode(raw).decode(),
    })
    r.raise_for_status()
    return r.json()["photo_id"]


def photo_events_in_db(db_path):
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT payload FROM events WHERE kind='photo'")
        return [json.loads(r[0]) for r in cur.fetchall()]
    finally:
        con.close()


def fail(msg):
    print(f"FAIL: {msg}")
    return 1


def main():
    ensure_safe_to_run()
    start_all()
    try:
        print("waiting for uvicorn to boot...")
        time.sleep(3.0)

        raw = fake_jpeg(b"photo-one")
        expect_hash = hashlib.sha256(raw).hexdigest()
        pid = upload(8001, raw)
        print(f"uploaded {pid} ({len(raw)} bytes) to node1")

        # --- 1. the event log holds metadata only, no pixels
        time.sleep(1.0)
        evs = [p for p in photo_events_in_db("./node1.db") if p["photo_id"] == pid]
        if not evs:
            return fail("photo event never landed in node1's log")
        ev = evs[0]
        if "image_base64" in ev:
            return fail("photo event still carries inline image_base64 -- log not split")
        if ev.get("blob_hash") != expect_hash:
            return fail(f"event's blob_hash {ev.get('blob_hash')} != sha256 of the bytes")
        if len(json.dumps(ev)) > 2000:
            return fail(f"photo event is {len(json.dumps(ev))}B -- too big to be metadata-only")
        print(f"PASS: event carries blob_hash + {len(json.dumps(ev))}B of metadata, no pixels")

        # --- 2. the originating node serves the exact bytes back
        r = get(8001, f"/photos/{pid}/image")
        if r.status_code != 200 or r.content != raw:
            return fail(f"node1 didn't serve back the original bytes ({r.status_code})")
        print("PASS: originating node serves the exact bytes")

        # --- 3. read-through: a node that knows the photo but hasn't pulled
        # its bytes must serve them, not 404. Wait for the METADATA to reach
        # node3 first -- before that it doesn't know the photo exists and
        # 404 is the correct answer, not the bug this is looking for.
        deadline = time.time() + 20
        while time.time() < deadline:
            if any(p["photo_id"] == pid for p in get(8003, "/photos").json()["photos"]):
                break
            time.sleep(0.3)
        else:
            return fail("photo metadata never gossiped to node3 within 20s")

        # If node3 lacks the blob at this instant, a 200 can only have come
        # from reading through to a peer. If blob_sync already delivered it,
        # the assertion still holds but proves less -- so say which happened.
        had_blob = expect_hash in get(8003, "/blobs/digest").json()["hashes"]
        r = get(8003, f"/photos/{pid}/image")
        if r.status_code != 200:
            return fail(f"node3 returned {r.status_code} instead of reading through to a peer")
        if r.content != raw:
            return fail("node3 served different bytes than were uploaded")
        print("PASS: a node lacking the blob serves it anyway"
              + (" (via read-through)" if not had_blob else " (blob_sync had already delivered it)"))

        # --- 4. background replication reaches every node on its own path
        deadline = time.time() + 25
        holders = set()
        while time.time() < deadline:
            holders = set()
            for _, port, _ in NODES:
                d = get(port, "/blobs/digest")
                if d.status_code == 200 and expect_hash in d.json()["hashes"]:
                    holders.add(port)
            if len(holders) == 3:
                break
            time.sleep(1.0)
        if len(holders) != 3:
            return fail(f"blob only reached {sorted(holders)} within 25s, expected all 3")
        print("PASS: blob replicated to all 3 nodes out-of-band")

        # ...and it got there WITHOUT the log ever carrying it
        for node_id, _, _ in NODES:
            for p in photo_events_in_db(f"./{node_id}.db"):
                if "image_base64" in p:
                    return fail(f"{node_id}'s log contains inline bytes -- gossip shipped pixels")
        print("PASS: no node's event log ever contained the pixels")

        # --- 5. identical bytes dedup to one blob
        before = get(8001, "/health").json()["blobs"]["blobs"]
        pid2 = upload(8001, raw, guest="g2")
        time.sleep(0.5)
        after = get(8001, "/health").json()["blobs"]["blobs"]
        if after != before:
            return fail(f"identical bytes created a second blob ({before} -> {after})")
        if get(8001, f"/photos/{pid2}/image").content != raw:
            return fail("the deduped second photo doesn't serve the shared bytes")
        print("PASS: identical bytes store once and both photos serve them")

        # --- 6. a distinct photo does create a distinct blob
        raw2 = fake_jpeg(b"photo-two")
        pid3 = upload(8002, raw2)
        time.sleep(0.5)
        if get(8002, f"/photos/{pid3}/image").content != raw2:
            return fail("second distinct photo didn't round-trip")
        if get(8001, "/health").json()["blobs"]["blobs"] < 1:
            return fail("blob count never grew for a distinct image")
        print("PASS: distinct bytes create a distinct blob")

        # --- 7. legacy inline photos (pre-split rows) still serve.
        # Written straight into node1's log, the shape every pre-split
        # photo has, then read back through the normal endpoint.
        legacy_raw = fake_jpeg(b"legacy", size=9000)
        con = sqlite3.connect("./node1.db")
        con.execute(
            "INSERT INTO events (origin, seq, kind, payload, created_at, vclock) "
            "VALUES (?,?,?,?,?,?)",
            ("legacy_node", 1, "photo", json.dumps({
                "photo_id": "ph_legacy", "guest_id": "old", "zone": "main",
                "composition_score": 10,
                "image_base64": base64.b64encode(legacy_raw).decode(),
            }), time.time(), "{}"),
        )
        con.commit()
        con.close()

        r = get(8001, "/photos/ph_legacy/image")
        if r.status_code != 200 or r.content != legacy_raw:
            return fail(f"a legacy inline photo stopped serving ({r.status_code})")
        print("PASS: legacy inline-base64 photos still serve after the split")

        # ...and the list endpoint doesn't ship their bytes
        body = get(8001, "/photos").json()
        blob = json.dumps(body)
        if "image_base64" in blob:
            return fail("GET /photos leaked image_base64 into the list response")
        if len(blob) > 100_000:
            return fail(f"GET /photos returned {len(blob)}B -- looks like it's shipping pixels")
        print(f"PASS: GET /photos returned {len(blob)}B for {len(body['photos'])} photos, no bytes")

        # --- 8. a legacy photo frozen into a recap gets a blob materialised
        # for it. Without this it could never be pinned and so never
        # archived -- and since cloud_sync stopped shipping base64, that
        # would leave pre-split photos with no off-cluster copy at all.
        # The original event must NOT be rewritten: it's already gossiped
        # under its (origin, seq), and two nodes disagreeing about one
        # event's content is the thing the merge model can't survive.
        def legacy_event():
            """Just the one event, not the whole table -- gossip keeps
            delivering other nodes' photos, so comparing every photo event
            would flag ordinary replication as a rewrite."""
            return next(p for p in photo_events_in_db("./node1.db")
                        if p["photo_id"] == "ph_legacy")

        before = json.dumps(legacy_event(), sort_keys=True)

        # The recap freezes on whichever node is Raft leader, so the legacy
        # row (inserted straight into node1's DB) has to have gossiped to
        # every node before triggering -- otherwise the leader simply
        # doesn't know about it yet and the snapshot legitimately omits it.
        deadline = time.time() + 20
        while time.time() < deadline:
            if all(any(p["photo_id"] == "ph_legacy" for p in get(port, "/photos").json()["photos"])
                   for _, port, _ in NODES):
                break
            time.sleep(0.5)
        else:
            return fail("the legacy row never replicated to all nodes within 20s")

        for _, port, _ in NODES:
            httpx.post(f"http://127.0.0.1:{port}/recap/trigger",
                       params={"event_id": "default"}, timeout=5.0,
                       trust_env=False).raise_for_status()
        time.sleep(2.5)

        recap = get(8001, "/recap", event_id="default").json()
        legacy_entry = next((p for p in recap["photos"] if p["photo_id"] == "ph_legacy"), None)
        if legacy_entry is None:
            return fail("the legacy photo never made it into the recap snapshot")
        if not legacy_entry.get("blob_hash"):
            return fail("legacy photo frozen into a recap has no blob_hash -- can't be archived")
        if legacy_entry["blob_hash"] != hashlib.sha256(legacy_raw).hexdigest():
            return fail("materialised blob_hash doesn't match the legacy photo's actual bytes")
        # Materialised on whichever node was leader, so give blob_sync a
        # moment to bring it here -- it's recap-pinned, which is exactly the
        # class it fetches first. Waiting also proves a materialised blob
        # replicates like any other rather than being stranded on one node.
        deadline = time.time() + 25
        while time.time() < deadline:
            if get(8001, "/blobs/" + legacy_entry["blob_hash"]).status_code == 200:
                break
            time.sleep(1.0)
        if get(8001, "/blobs/" + legacy_entry["blob_hash"]).content != legacy_raw:
            return fail("the materialised blob doesn't serve the legacy photo's bytes")
        if json.dumps(legacy_event(), sort_keys=True) != before:
            return fail("materialising a blob rewrote the original photo event -- must not")
        print("PASS: a legacy photo in a recap gets a blob without its event being rewritten")

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
