"""Quorum-read test: spins up 3 nodes, isolates one with a full bidirectional
chaos partition, and confirms the CAP tradeoff /zones/quorum exposes is real:

  - R=2 (majority of N=3) always returns the fresh/complete view, because
    any 2-of-3 sample is guaranteed to include a node outside a single
    -node partition.
  - R=1 sometimes lands on the partitioned node and returns an undercount
    -- observable, not asserted on faith.
  - Healing the partition and letting gossip catch up makes R=1 reliably
    fresh again.

Self-contained: starts/kills its own processes, cleans up its own .db
files.
"""
import subprocess
import sys
import time
import os
import httpx

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


def post_photo(port, zone, guest="g1"):
    r = httpx.post(f"http://127.0.0.1:{port}/photos", json={
        "guest_id": guest, "zone": zone, "composition_score": 90,
    }, timeout=2.0)
    r.raise_for_status()
    return r.json()["photo_id"]


def quorum(port, R, W=2):
    r = httpx.get(f"http://127.0.0.1:{port}/zones/quorum", params={"R": R, "W": W}, timeout=2.0)
    r.raise_for_status()
    return r.json()


def has_zone(result, zone):
    return any(z["zone"] == zone for z in result["zones"])


def main():
    start_all()
    try:
        print("waiting for uvicorn to boot...")
        time.sleep(2.0)

        post_photo(8001, "flower_arch")
        post_photo(8001, "bar")
        time.sleep(2.0)  # let the baseline converge everywhere

        baseline = quorum(8001, R=3)
        print("baseline (R=3, all nodes) zones:", [z["zone"] for z in baseline["zones"]])
        if not (has_zone(baseline, "flower_arch") and has_zone(baseline, "bar")):
            print("FAIL: baseline never converged before partitioning")
            return 1

        # Fully isolate node3, both directions, from both other nodes --
        # a one-sided partition just delays convergence, it doesn't hold.
        print("partitioning node3 (bidirectional, from all sides)...")
        httpx.post("http://127.0.0.1:8001/chaos/partition/1", timeout=2.0)  # node1 -> node3
        httpx.post("http://127.0.0.1:8002/chaos/partition/1", timeout=2.0)  # node2 -> node3
        httpx.post("http://127.0.0.1:8003/chaos/partition/0", timeout=2.0)  # node3 -> node1
        httpx.post("http://127.0.0.1:8003/chaos/partition/1", timeout=2.0)  # node3 -> node2

        # New write after the partition: reaches node1 and (via gossip)
        # node2, never reaches the isolated node3.
        post_photo(8001, "new_zone")
        time.sleep(2.0)

        r2_zones = quorum(8002, R=3)  # sanity: node2 got it
        if not has_zone(r2_zones, "new_zone"):
            print("FAIL: node2 never received the post-partition write")
            return 1

        print("querying R=1 repeatedly (should sometimes land on stale node3)...")
        r1_results = [has_zone(quorum(8001, R=1), "new_zone") for _ in range(30)]
        fresh_count = sum(r1_results)
        print(f"R=1: fresh {fresh_count}/30, stale {30 - fresh_count}/30")
        if fresh_count == 30:
            print("FAIL: R=1 never landed on the partitioned node in 30 tries (too unlucky, or broken)")
            return 1
        if fresh_count == 0:
            print("FAIL: R=1 always landed on the partitioned node (sampling isn't random)")
            return 1
        print("PASS: R=1 shows both fresh and stale reads -- CAP tradeoff is real, not asserted")

        print("querying R=2 repeatedly (should always be fresh -- majority excludes the stale node)...")
        r2_results = [has_zone(quorum(8001, R=2), "new_zone") for _ in range(20)]
        if not all(r2_results):
            print(f"FAIL: R=2 went stale {r2_results.count(False)}/20 times -- quorum majority broken")
            return 1
        print("PASS: R=2 always fresh (20/20)")

        print("healing partition...")
        httpx.post("http://127.0.0.1:8001/chaos/heal", timeout=2.0)
        httpx.post("http://127.0.0.1:8002/chaos/heal", timeout=2.0)
        httpx.post("http://127.0.0.1:8003/chaos/heal", timeout=2.0)
        time.sleep(3.0)  # a few gossip rounds to let node3 catch up

        post_heal = [has_zone(quorum(8001, R=1), "new_zone") for _ in range(10)]
        print(f"post-heal R=1 fresh: {sum(post_heal)}/10")
        if not all(post_heal):
            print("FAIL: still going stale after heal -- node3 never caught up")
            return 1
        print("PASS: self-corrected after heal, R=1 consistently fresh again")

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
