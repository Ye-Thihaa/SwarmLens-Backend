"""POST /analyze/preview test: the live-viewfinder path.

This endpoint had two bugs reach a phone, and both survived a check that
looked like it covered them, so the assertions here are chosen to be the
ones that would actually have failed:

  1. The guidance direction was inverted near a rule-of-thirds line. A
     browser test "passed" because its subject sat far enough off-centre
     to escape the bias. So: subjects are placed at several known
     positions, including ones only modestly off-centre, and the reported
     direction is compared against the direction computed independently
     from the truth -- not eyeballed.

  2. move_subject_* and pan_camera_* must stay exact opposites. Rendering
     the wrong one is correct arithmetic turned into a backwards arrow,
     which is the failure this whole feature exists to avoid.

Plus the property the endpoint exists for: it must persist NOTHING. A
viewfinder calls it every 1.5s against frames nobody shot; if those ever
start landing in the event log they will flood gossip and skew /zones.

One node is enough -- there is no replication behaviour here to test.
"""
import base64
import io
import os
import subprocess
import sys
import time

import httpx
import numpy as np
from PIL import Image

from testutil import ensure_safe_to_run

PORT = 8001
DB = "./node1.db"
BASE = f"http://127.0.0.1:{PORT}"
W, H = 480, 640          # a portrait frame, like a phone
ASPECT = W / H

proc = None


def start_node():
    global proc
    env = os.environ.copy()
    env.update({
        "NODE_ID": "node1", "DB_PATH": DB,
        "SELF_URL": BASE, "PEERS": "", "GOSSIP_INTERVAL": "1.0",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            httpx.get(f"{BASE}/health", timeout=1.0, trust_env=False)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def subject_frame(cx_frac: float, cy_frac: float) -> str:
    """A bright blob on a dark ground at a known normalised position."""
    a = np.zeros((H, W, 3), dtype=np.uint8)
    a[:, :] = (28, 34, 44)
    cx, cy = int(cx_frac * W), int(cy_frac * H)
    r = 46
    y0, y1 = max(0, cy - r), min(H, cy + r)
    x0, x1 = max(0, cx - r), min(W, cx + r)
    a[y0:y1, x0:x1] = (250, 240, 205)
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def preview(b64, aspect=ASPECT, timeout=120.0):
    return httpx.post(f"{BASE}/analyze/preview",
                      json={"image_base64": b64, "aspect": aspect},
                      timeout=timeout, trust_env=False)


def expected_move(pos: float) -> str:
    """Independently: which way must the subject shift to reach the nearest
    thirds line? Computed from the truth, not from the engine's answer."""
    target = min((1 / 3, 2 / 3), key=lambda t: abs(t - pos))
    d = target - pos
    if abs(d) < 0.03:          # WELL_COMPOSED_EPS
        return "centered"
    return "right" if d > 0 else "left"


def events_now():
    return httpx.get(f"{BASE}/health", timeout=3.0, trust_env=False).json()["events"]


def main():
    ensure_safe_to_run(ports=(PORT,), db_paths=[DB])
    if not start_node():
        print("FAIL: node never came up")
        return 1
    failures = 0
    try:
        print("warming models (first call loads ~154MB from disk)...")
        r = preview(subject_frame(0.5, 0.5))
        r.raise_for_status()

        before = events_now()

        # --- 1. direction correctness at several known positions ---
        # 0.28 and 0.72 are the important ones: modestly off-centre, close
        # enough to a thirds line that a centre-biased detector flips them.
        print("\nchecking guidance direction against known subject positions")
        for pos in (0.12, 0.28, 0.45, 0.55, 0.72, 0.88):
            body = preview(subject_frame(pos, 0.5)).json()
            g = body["ar_guide"]
            want = expected_move(pos)
            got = g["move_subject_x"]
            ok = got == want
            failures += not ok
            print(f"  x={pos:.2f}  want move_subject_x={want:<8} got={got:<8} "
                  f"pan_camera_x={g['pan_camera_x']:<8} {'ok' if ok else 'FAIL'}")

        # --- 2. the two direction fields must be exact opposites ---
        print("\nchecking move_subject_* and pan_camera_* stay inverses")
        opposite = {"left": "right", "right": "left",
                    "up": "down", "down": "up", "centered": "centered"}
        bad = 0
        for pos in (0.15, 0.5, 0.85):
            g = preview(subject_frame(pos, pos)).json()["ar_guide"]
            for axis in ("x", "y"):
                if g[f"pan_camera_{axis}"] != opposite[g[f"move_subject_{axis}"]]:
                    print(f"  FAIL: at {pos}, {axis}: move={g[f'move_subject_{axis}']} "
                          f"pan={g[f'pan_camera_{axis}']} are not opposites")
                    bad += 1
        failures += bad
        print("  ok" if not bad else f"  {bad} mismatches")

        # --- 3. the whole point: nothing is persisted ---
        print("\nchecking the endpoint writes no events")
        for _ in range(6):
            preview(subject_frame(0.3, 0.7))
        after = events_now()
        if after != before:
            print(f"  FAIL: event count moved {before} -> {after}; preview must persist nothing")
            failures += 1
        else:
            print(f"  ok ({after} events before and after 6 more previews)")

        # --- 4. response shape the client depends on ---
        print("\nchecking response shape")
        body = preview(subject_frame(0.2, 0.3)).json()
        required = {"ar_guide", "reframe", "scene", "suggested_filter",
                    "confident", "margin", "picks", "reason", "aesthetic_score"}
        missing = required - set(body)
        if missing:
            print(f"  FAIL: missing keys {missing}")
            failures += 1
        elif body["reframe"]["rect"]["w"] <= 0 or body["reframe"]["zoom"] < 1.0:
            print(f"  FAIL: nonsensical reframe {body['reframe']}")
            failures += 1
        else:
            print(f"  ok (source={body['ar_guide']['subject_source']}, "
                  f"zoom={body['reframe']['zoom']}x, confident={body['confident']})")

        # --- 5. input guards, since this is called on a loop ---
        print("\nchecking input guards")
        checks = [
            ("oversize body", {"image_base64": "A" * (2 * 1024 * 1024 + 10)}, 413),
            ("not base64", {"image_base64": "!!!!"}, 400),
            ("absurd aspect", {"image_base64": subject_frame(0.5, 0.5), "aspect": 99.0}, 400),
        ]
        for label, payload, want_code in checks:
            code = httpx.post(f"{BASE}/analyze/preview", json=payload,
                              timeout=30.0, trust_env=False).status_code
            ok = code == want_code
            failures += not ok
            print(f"  {label:<14} want {want_code} got {code} {'ok' if ok else 'FAIL'}")

        print("\n" + ("PASS: all preview checks" if not failures
                      else f"FAIL: {failures} check(s) failed"))
        return 1 if failures else 0
    finally:
        if proc:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            os.remove(DB)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    sys.exit(main())
