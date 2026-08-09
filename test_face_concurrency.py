"""Face detection must survive being called from two threads at once.

Pure in-process test -- it imports ai_engine directly and starts no nodes,
so unlike every other test_*.py here it does NOT need ports 8001-8003 or
the node*.db files, and is safe to run against a live demo cluster. (It
does need ./models/ populated; a cold run downloads them.)

Why this exists. ai_engine caches one FaceDetectorYN and one MediaPipe
FaceLandmarker for the whole process, and main.py hands both /analyze and
/analyze/preview to asyncio.to_thread with PREVIEW_CONCURRENCY previews
allowed at once. client-2 posts /analyze the moment a photo syncs while
the capture screen's preview loop is still ticking, so two threads inside
the detector is ordinary behaviour, not a stress case.

Before the locks in ai_engine.py, this reproduced two distinct failures:

  * different frame sizes -- "Unknown C++ exception from OpenCV code",
    on the first iteration, every time.
  * identical frame sizes -- "(-215:Assertion failed) buf.shape() ==
    m.shape() in function 'forwardGraph'", and separately a run that
    returned zero faces for a frame containing two.

That last one is the reason this test asserts on *results* and not only on
the absence of exceptions. An empty detection is not an obvious failure --
it is exactly the signal find_subject() reads as "no face here, use
saliency", so a corrupted read is silently indistinguishable from a real
answer, and the guest gets a confident box around the wrong thing.
"""

import io
import os
import sys
import threading

import cv2
import numpy as np
from PIL import Image

import ai_engine

# A committed asset rather than a synthetic image: this needs real faces to
# be able to notice a detection going quietly empty, and a drawn one won't
# reliably produce any. Two people against a bright window -- also the frame
# where face-first beats saliency most clearly.
FIXTURE = os.path.join("client-2", "src", "assets", "frame-window.jpg")

ITERATIONS = 150


def frame(edge: int) -> np.ndarray:
    """The fixture at a given long edge, JPEG'd the way capture.tsx sends it."""
    img = Image.open(FIXTURE).convert("RGB")
    w, h = img.size
    s = edge / max(w, h)
    img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    out = Image.open(io.BytesIO(buf.getvalue()))
    out.load()
    return cv2.cvtColor(np.asarray(out.convert("RGB")), cv2.COLOR_RGB2BGR)


def same(a, b) -> bool:
    if len(a) != len(b):
        return False
    return all(np.allclose(x[:4], y[:4], atol=1.0) for x, y in zip(a, b))


def hammer(label: str, img: np.ndarray, out: dict, errors: list) -> None:
    got = []
    out[label] = got
    for _ in range(ITERATIONS):
        try:
            got.append(ai_engine._detect_faces(img))
        except Exception as e:  # noqa: BLE001 -- the failure mode under test
            errors.append(f"{label}: {type(e).__name__}: {str(e).strip()[:120]}")
            return


def run_pair(name: str, a_img: np.ndarray, b_img: np.ndarray) -> bool:
    """Two threads, one frame each, against each frame's own known-good result."""
    truth = {"a": ai_engine._detect_faces(a_img), "b": ai_engine._detect_faces(b_img)}
    out: dict = {}
    errors: list = []

    threads = [
        threading.Thread(target=hammer, args=("a", a_img, out, errors)),
        threading.Thread(target=hammer, args=("b", b_img, out, errors)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = True
    print(f"\n{name}")
    for label in ("a", "b"):
        runs = out.get(label, [])
        wrong = [r for r in runs if not same(r, truth[label])]
        print(f"  thread {label}: {len(runs)}/{ITERATIONS} completed, "
              f"{len(wrong)} disagreed with the single-threaded result "
              f"({len(truth[label])} face(s))")
        if len(runs) != ITERATIONS or wrong:
            ok = False
    if errors:
        ok = False
        for e in errors:
            print(f"  EXCEPTION {e}")
    return ok


def main() -> None:
    if not os.path.exists(FIXTURE):
        print(f"missing fixture {FIXTURE} -- run this from the repo root")
        sys.exit(2)

    small, large = frame(224), frame(1280)
    print(f"fixture {FIXTURE}")
    print(f"  224px  -> {len(ai_engine._detect_faces(small))} face(s)")
    print(f"  1280px -> {len(ai_engine._detect_faces(large))} face(s)")

    # The real pairing: /analyze sends a full capture while /analyze/preview
    # sends a 224px viewfinder frame. Different sizes, so the shared
    # detector's input size is contended as well as its buffers.
    mixed = run_pair("mixed sizes (/analyze alongside /analyze/preview)", large, small)
    # Two guests previewing on one node: same size, so only the internal
    # buffers are contended. This one still failed before the fix.
    equal = run_pair("identical sizes (two guests previewing at once)", small, frame(224))

    if mixed and equal:
        print("\nOK -- concurrent detection is consistent with single-threaded results")
    else:
        print("\nFAILED -- the shared face detector is not concurrency-safe")
        sys.exit(1)


if __name__ == "__main__":
    main()
