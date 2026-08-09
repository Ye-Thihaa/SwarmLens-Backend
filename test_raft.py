import subprocess
import sys
import time
import os
import sqlite3
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


def get_status(port):
    try:
        # trust_env=False: this is always 127.0.0.1 -- a system HTTP
        # proxy would otherwise intercept loopback traffic and produce
        # 502s / resets that this try/except silently swallows as "node
        # unreachable", which is a very confusing false signal.
        r = httpx.get(f"http://127.0.0.1:{port}/raft/status", timeout=1.0, trust_env=False)
        return r.json()
    except Exception:
        return None


def wait_for_leader(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        statuses = {nid: get_status(port) for nid, (p, port) in procs.items()}
        leaders = [nid for nid, s in statuses.items() if s and s["role"] == "leader"]
        if len(leaders) == 1:
            return leaders[0], statuses
        time.sleep(0.05)
    return None, {nid: get_status(port) for nid, (p, port) in procs.items()}


def recap_count_in_db(db_path):
    if not os.path.exists(db_path):
        return 0
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT COUNT(*) FROM events WHERE kind='recap_sent'")
        return cur.fetchone()[0]
    finally:
        con.close()


def main():
    ensure_safe_to_run()
    start_all()
    try:
        print("waiting for uvicorn to boot...")
        time.sleep(2.0)

        leader, statuses = wait_for_leader(timeout=10)
        print("initial statuses:", statuses)
        if not leader:
            print("FAIL: no single leader elected")
            return 1
        print(f"PASS: leader elected -> {leader}")

        # Broadcast the recap trigger to all three nodes, as a real client
        # would (it doesn't know who the leader is). Only the leader acts.
        print("triggering recap on all nodes...")
        trig_results = {}
        for nid, (p, port) in procs.items():
            r = httpx.post(f"http://127.0.0.1:{port}/recap/trigger", timeout=2.0, trust_env=False)
            trig_results[nid] = r.json()
        print("trigger results:", trig_results)

        time.sleep(2.0)  # let gossip converge the recap_sent event

        pre_kill_counts = {nid: recap_count_in_db(f"./{nid}.db") for nid, _ in procs.items()}
        print("recap_sent counts across nodes (pre-kill):", pre_kill_counts)
        if sum(pre_kill_counts.values()) == 0:
            print("FAIL: recap never fired")
            return 1

        leader_proc, leader_port = procs[leader]

        t0 = time.time()
        print(f"killing leader {leader} (pid={leader_proc.pid}) ...")
        leader_proc.kill()
        leader_proc.wait(timeout=5)
        del procs[leader]

        new_leader, statuses = wait_for_leader(timeout=5)
        elapsed = time.time() - t0
        print(f"post-kill statuses ({elapsed:.3f}s after kill):", statuses)

        if not new_leader:
            print(f"FAIL: no new leader elected within {elapsed:.3f}s")
            return 1
        if new_leader == leader:
            print("FAIL: 'new' leader is the same as the killed one")
            return 1
        print(f"PASS: re-elected {new_leader} in {elapsed:.3f}s (budget ~1s)")

        # Broadcast trigger again post-election (idempotency check: must
        # NOT create a second recap_sent event even from the new leader).
        for nid, (p, port) in procs.items():
            httpx.post(f"http://127.0.0.1:{port}/recap/trigger", timeout=2.0, trust_env=False)

        time.sleep(2.0)  # let gossip converge across survivors
        final_counts = {nid: recap_count_in_db(f"./{nid}.db") for nid, _ in procs.items()}
        print("recap_sent counts across surviving nodes (final):", final_counts)

        total_distinct_origins = set()
        for nid, _ in procs.items():
            con = sqlite3.connect(f"./{nid}.db")
            cur = con.execute("SELECT DISTINCT origin FROM events WHERE kind='recap_sent'")
            for row in cur.fetchall():
                total_distinct_origins.add(row[0])
            con.close()
        print("distinct recap_sent origins seen cluster-wide:", total_distinct_origins)

        if len(total_distinct_origins) != 1:
            print(f"FAIL: expected exactly 1 recap_sent origin, got {len(total_distinct_origins)}")
            return 1
        if any(c != 1 for c in final_counts.values()):
            print("FAIL: some surviving node doesn't have exactly one recap_sent row")
            return 1

        print("PASS: recap fired exactly once cluster-wide, survives leader crash + re-election")
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


if __name__ == "__main__":
    sys.exit(main())
