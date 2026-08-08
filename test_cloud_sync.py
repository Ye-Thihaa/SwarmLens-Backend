"""Cloud archive sync test: no real Supabase project exists for this repo,
so a tiny in-process fake "cloud" HTTP server stands in for one -- it
accepts the same PostgREST-style POST a real Supabase table would, and
dedups on (origin, seq) the same way a UNIQUE constraint + `Prefer:
resolution=ignore-duplicates` would on the real thing.

Confirms:
  - while the cloud is unreachable, everything else (photos, health,
    zones) keeps working -- cloud sync never blocks the cluster
  - POST /cloud_sync/trigger against an unreachable cloud fails softly
    (synced: 0, last_error set), not a 500
  - once the cloud comes back, the full backlog syncs in one push
  - re-triggering with nothing new pushes 0 (checkpoint advanced
    correctly) and doesn't duplicate anything at the destination
  - a genuinely new event after that syncs incrementally, not a full resend
  - only the Raft leader ever pushes; a follower's trigger is a no-op

Self-contained: starts/kills its own node processes and the fake cloud
server, cleans up its own .db files.
"""
import http.server
import json
import subprocess
import sys
import threading
import time
import os
import httpx

NODES = [
    ("node1", 8001, "http://127.0.0.1:8002,http://127.0.0.1:8003"),
    ("node2", 8002, "http://127.0.0.1:8001,http://127.0.0.1:8003"),
    ("node3", 8003, "http://127.0.0.1:8001,http://127.0.0.1:8002"),
]
FAKE_KEY = "test-key"

procs = {}
fake_cloud_rows: dict[tuple, dict] = {}
fake_cloud_lock = threading.Lock()
fake_cloud_request_count = 0


class FakeCloudHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global fake_cloud_request_count
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.headers.get("apikey") != FAKE_KEY:
            self.send_response(401)
            self.end_headers()
            return
        rows = json.loads(body)
        with fake_cloud_lock:
            fake_cloud_request_count += 1
            for row in rows:
                fake_cloud_rows[(row["origin"], row["seq"])] = row
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, format, *args):
        pass


def start_all(supabase_url, fake_port):
    for node_id, port, peers in NODES:
        env = os.environ.copy()
        env["NODE_ID"] = node_id
        env["DB_PATH"] = f"./{node_id}.db"
        env["SELF_URL"] = f"http://127.0.0.1:{port}"
        env["PEERS"] = peers
        env["GOSSIP_INTERVAL"] = "1.0"
        env["SUPABASE_URL"] = supabase_url
        env["SUPABASE_KEY"] = FAKE_KEY
        env["SUPABASE_EVENTS_TABLE"] = "events"
        env["CLOUD_SYNC_INTERVAL"] = "3600"  # only explicit /trigger calls matter in this test
        # The fake cloud below is local (127.0.0.1) even though
        # cloud_sync.py defaults to trusting a system proxy (correct for
        # a real, external Supabase) -- override it for this test only,
        # same reasoning as every other trust_env=False in this file.
        env["CLOUD_SYNC_TRUST_ENV"] = "false"
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs[node_id] = (p, port)
    print("started:", {k: v[1] for k, v in procs.items()})


def find_leader():
    # trust_env=False throughout this file: node calls are always
    # 127.0.0.1 -- a system HTTP proxy would otherwise intercept loopback
    # traffic. (The fake cloud server also binds 127.0.0.1, same reason.)
    for nid, (p, port) in procs.items():
        r = httpx.get(f"http://127.0.0.1:{port}/raft/status", timeout=2.0, trust_env=False).json()
        if r["role"] == "leader":
            return nid, port
    return None, None


def post_photo(port, zone):
    r = httpx.post(f"http://127.0.0.1:{port}/photos", json={
        "guest_id": "g1", "zone": zone, "composition_score": 90,
    }, timeout=2.0, trust_env=False)
    r.raise_for_status()


def trigger(port):
    r = httpx.post(f"http://127.0.0.1:{port}/cloud_sync/trigger", timeout=5.0, trust_env=False)
    r.raise_for_status()
    return r.json()


def main():
    # reserve a free port for the fake cloud but don't start it yet --
    # that's "disconnected from the internet"
    probe = http.server.HTTPServer(("127.0.0.1", 0), FakeCloudHandler)
    fake_port = probe.server_address[1]
    probe.server_close()
    supabase_url = f"http://127.0.0.1:{fake_port}"

    start_all(supabase_url, fake_port)
    try:
        print("waiting for uvicorn to boot...")
        time.sleep(2.0)

        leader_id, leader_port = find_leader()
        print(f"leader is {leader_id} on :{leader_port}")

        post_photo(leader_port, "flower_arch")
        post_photo(leader_port, "bar")
        time.sleep(1.5)  # let gossip converge first

        # --- disconnected: cluster keeps working, sync fails softly ---
        print("cloud is not listening yet (simulating no internet)...")
        result = trigger(leader_port)
        print("trigger while disconnected:", result)
        if result["synced"] != 0 or not result["last_error"]:
            print(f"FAIL: expected a soft failure (synced=0, last_error set), got {result}")
            return 1
        h = httpx.get(f"http://127.0.0.1:{leader_port}/health", timeout=2.0, trust_env=False).json()
        if h["events"] < 2:
            print("FAIL: core functionality should be unaffected while cloud is unreachable")
            return 1
        print("PASS: cluster keeps working, trigger fails softly (no 500) while cloud is down")

        # --- reconnect: backlog syncs ---
        print("starting fake cloud (reconnecting)...")
        server = http.server.ThreadingHTTPServer(("127.0.0.1", fake_port), FakeCloudHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        result = trigger(leader_port)
        print("trigger after reconnect:", result)
        if result["synced"] < 2 or result["last_error"]:
            print(f"FAIL: expected the backlog to sync cleanly, got {result}")
            return 1
        node_event_count = httpx.get(f"http://127.0.0.1:{leader_port}/health", timeout=2.0, trust_env=False).json()["events"]
        with fake_cloud_lock:
            cloud_count = len(fake_cloud_rows)
        if cloud_count != node_event_count:
            print(f"FAIL: fake cloud has {cloud_count} rows, leader has {node_event_count} events")
            return 1
        print(f"PASS: backlog synced automatically on reconnect ({cloud_count} events)")

        # --- re-trigger with nothing new: 0 synced, no duplicates ---
        result = trigger(leader_port)
        with fake_cloud_lock:
            cloud_count_after = len(fake_cloud_rows)
        if result["synced"] != 0 or cloud_count_after != cloud_count:
            print(f"FAIL: expected a no-op re-trigger, got synced={result['synced']}, "
                  f"cloud rows {cloud_count} -> {cloud_count_after}")
            return 1
        print("PASS: re-triggering with nothing new is a true no-op, no duplicates")

        # --- one more real event: incremental, not a full resend ---
        post_photo(leader_port, "dance_floor")
        time.sleep(0.2)
        result = trigger(leader_port)
        if result["synced"] != 1:
            print(f"FAIL: expected exactly 1 new event synced incrementally, got {result['synced']}")
            return 1
        print("PASS: a new event syncs incrementally, not a full resend")

        # --- a follower's trigger is a no-op ---
        follower_port = next(port for nid, (p, port) in procs.items() if port != leader_port)
        result = trigger(follower_port)
        if result["synced"] != 0 or result["role"] == "leader":
            print(f"FAIL: a follower should never push, got {result}")
            return 1
        print("PASS: only the leader pushes; a follower's trigger is a no-op")

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
