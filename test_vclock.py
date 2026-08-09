"""Vector clock test: no guest client exists yet (Phase 5's frontend half),
so this simulates a couple of "devices" attaching {device_id: counter}
clocks to POST /photos directly, and confirms:

  - two photos from independent devices, neither aware of the other, are
    flagged concurrent_with each other
  - a photo whose clock causally *includes* an earlier photo's clock is
    NOT flagged concurrent with it (happened-after, correctly ordered)
  - a photo posted with no vclock at all (every photo posted by every
    other test/demo in this repo) is never flagged concurrent with
    anything -- no causal info means no claim, not a default "concurrent"
  - the vclock field (and the concurrent_with relationships it implies)
    survives gossip replication to the other two nodes

Self-contained: starts/kills its own processes, cleans up its own .db
files.
"""
import subprocess
import sys
import time
import os
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
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs[node_id] = (p, port)
    print("started:", {k: v[1] for k, v in procs.items()})


def post_photo(port, zone, vclock=None):
    # trust_env=False throughout this file: every call targets 127.0.0.1
    # -- a system HTTP proxy would otherwise intercept loopback traffic.
    r = httpx.post(f"http://127.0.0.1:{port}/photos", json={
        "guest_id": "g1", "zone": zone, "composition_score": 90,
        "vclock": vclock or {},
    }, timeout=2.0, trust_env=False)
    r.raise_for_status()
    return r.json()["photo_id"]


def photos(port):
    r = httpx.get(f"http://127.0.0.1:{port}/photos", timeout=2.0, trust_env=False)
    return {p["photo_id"]: p for p in r.json()["photos"]}


def main():
    ensure_safe_to_run()
    start_all()
    try:
        print("waiting for uvicorn to boot...")
        time.sleep(2.0)

        # device A takes a photo, knowing only its own history
        pid1 = post_photo(8001, "flower_arch", vclock={"deviceA": 1})
        # device B takes a photo independently -- no knowledge of A at all
        pid2 = post_photo(8001, "bar", vclock={"deviceB": 1})
        # device B takes a second photo, this time *after* having seen A's
        # (e.g. synced/merged locally first) -- its clock now dominates A's
        pid3 = post_photo(8001, "bar", vclock={"deviceA": 1, "deviceB": 2})
        # no vclock at all -- e.g. an old client, or any other test in this repo
        pid_bare = post_photo(8001, "dance_floor")

        p = photos(8001)
        print("pid1 concurrent_with:", p[pid1]["concurrent_with"])
        print("pid2 concurrent_with:", p[pid2]["concurrent_with"])
        print("pid3 concurrent_with:", p[pid3]["concurrent_with"])
        print("pid_bare concurrent_with:", p[pid_bare]["concurrent_with"])

        if pid2 not in p[pid1]["concurrent_with"] or pid1 not in p[pid2]["concurrent_with"]:
            print("FAIL: independent devices' photos should be mutually concurrent")
            return 1
        if pid1 in p[pid3]["concurrent_with"] or pid3 in p[pid1]["concurrent_with"]:
            print("FAIL: pid3's clock causally dominates pid1's -- should NOT be concurrent")
            return 1
        if p[pid_bare]["concurrent_with"]:
            print("FAIL: a photo with no vclock should never be flagged concurrent with anything")
            return 1
        print("PASS: concurrency detection matches the causal structure, not arrival order")

        print("waiting for gossip round...")
        time.sleep(2.0)
        p3 = photos(8003)
        if sorted(p3[pid1]["concurrent_with"]) != sorted(p[pid1]["concurrent_with"]):
            print(f"FAIL: node3's concurrent_with for pid1 disagrees with node1's: "
                  f"{p3[pid1]['concurrent_with']} vs {p[pid1]['concurrent_with']}")
            return 1
        if p3[pid1]["vclock"] != {"deviceA": 1}:
            print(f"FAIL: vclock didn't survive gossip replication to node3: {p3[pid1]['vclock']}")
            return 1
        print("PASS: vclock and the concurrency relationships it implies replicate via gossip")

        return 0
    finally:
        print("cleaning up remaining processes...")
        for nid, (proc, port) in list(procs.items()):
            proc.kill()
        for nid, (proc, port) in list(procs.items()):
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        for node_id, _, _ in NODES:
            try:
                os.remove(f"./{node_id}.db")
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    sys.exit(main())
