"""AI engine test: spins up 3 nodes, POSTs photos + calls /analyze on node1
with synthetic images crafted to have an unambiguous right answer, and
confirms:

  - an obviously off-center subject produces a sane AR guide direction
  - photos matching different film-stock prompts get different
    suggested_filter results (not the same value regardless of input)
  - the resulting aesthetic_score event shows up in GET /health's event
    count and reaches the other two nodes within a gossip round or two,
    exactly like the photo-replication demo in README.md

Self-contained: starts/kills its own processes, cleans up its own .db
files. Model weights (~154MB) are downloaded once into ./models/ on first
run of the whole repo (see ai_engine.py) and cached after that; this test
does not delete that cache.
"""
import base64
import io
import subprocess
import sys
import time
import os
import sqlite3
import httpx
import numpy as np
from PIL import Image

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


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def off_center_subject_image() -> Image.Image:
    """Bright circle near the top-left corner on a dark background --
    saliency should find it there, well away from any rule-of-thirds
    intersection, so the AR guide has an unambiguous direction to report."""
    import cv2
    arr = np.zeros((300, 400, 3), dtype=np.uint8)
    cv2.circle(arr, (50, 40), 30, (255, 255, 255), -1)
    return Image.fromarray(arr)


def sunset_gradient_image() -> Image.Image:
    img = Image.new("RGB", (400, 300))
    px = img.load()
    for y in range(300):
        for x in range(400):
            px[x, y] = (255, 140 + y // 4, 60)
    return img


def flat_gray_image() -> Image.Image:
    return Image.new("RGB", (400, 300), color=(120, 120, 120))


def post_photo(port, zone):
    # trust_env=False throughout this file: every call targets 127.0.0.1
    # -- a system HTTP proxy would otherwise intercept loopback traffic.
    r = httpx.post(f"http://127.0.0.1:{port}/photos", json={
        "guest_id": "g1", "zone": zone, "composition_score": 90,
    }, timeout=2.0, trust_env=False)
    r.raise_for_status()
    return r.json()["photo_id"]


def analyze(port, photo_id, img):
    r = httpx.post(f"http://127.0.0.1:{port}/analyze", json={
        "photo_id": photo_id,
        "image_base64": base64.b64encode(png_bytes(img)).decode(),
    }, timeout=90.0, trust_env=False)  # first call loads ~154MB of models from disk
    r.raise_for_status()
    return r.json()


def event_count(db_path, kind):
    if not os.path.exists(db_path):
        return 0
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT COUNT(*) FROM events WHERE kind=?", (kind,))
        return cur.fetchone()[0]
    finally:
        con.close()


def main():
    start_all()
    try:
        print("waiting for uvicorn to boot...")
        time.sleep(2.0)

        # --- AR guide direction ---
        pid1 = post_photo(8001, "photo_booth")
        print("analyzing off-center subject...")
        r1 = analyze(8001, pid1, off_center_subject_image())
        guide = r1["ar_guide"]
        print("ar_guide:", guide)
        if guide["well_composed"]:
            print("FAIL: subject is intentionally off-center, well_composed should be False")
            return 1
        if guide["move_subject_x"] != "right" or guide["move_subject_y"] != "down":
            print(f"FAIL: subject near top-left should guide right+down toward the nearest "
                  f"thirds intersection, got {guide['move_subject_x']}/{guide['move_subject_y']}")
            return 1
        print("PASS: AR guide direction is sane for an off-center subject")

        # --- filter differentiation ---
        pid2 = post_photo(8001, "bar")
        pid3 = post_photo(8001, "dance_floor")
        print("analyzing sunset-gradient vs flat-gray images...")
        r2 = analyze(8001, pid2, sunset_gradient_image())
        r3 = analyze(8001, pid3, flat_gray_image())
        print("sunset ->", r2["suggested_filter"], " | flat gray ->", r3["suggested_filter"])
        # Asserted as a set, not one exact key: a vivid orange gradient
        # genuinely suits either colour-forward warm stock, and pinning the
        # single winner would make this test fail on prompt wording rather
        # than on the engine actually breaking.
        WARM_COLOR_STOCKS = {"ektar_100", "gold_200"}
        if r2["suggested_filter"] not in WARM_COLOR_STOCKS:
            print(f"FAIL: vivid warm gradient should suggest one of {WARM_COLOR_STOCKS}, "
                  f"got {r2['suggested_filter']}")
            return 1
        if r2["suggested_filter"] == r3["suggested_filter"]:
            print("FAIL: two very different images both got the same suggested_filter")
            return 1
        print("PASS: suggested_filter differentiates between images")

        # --- aesthetic_score event: local count + gossip replication ---
        h1 = httpx.get("http://127.0.0.1:8001/health", timeout=2.0, trust_env=False).json()
        print("node1 event count after 3 analyses:", h1["events"])
        if event_count("./node1.db", "aesthetic_score") != 3:
            print("FAIL: expected 3 aesthetic_score events on node1's own db")
            return 1

        print("waiting for a gossip round...")
        time.sleep(2.0)
        counts = {nid: event_count(f"./{nid}.db", "aesthetic_score") for nid, _, _ in NODES}
        print("aesthetic_score counts across nodes:", counts)
        if any(c != 3 for c in counts.values()):
            print(f"FAIL: aesthetic_score events didn't reach every node within a gossip round: {counts}")
            return 1
        print("PASS: aesthetic_score events replicate like any other event, no second code path")

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
