"""Pretrained-only photo analysis: saliency -> AR reframe guide, CLIP ->
film-stock suggestion, CLIP + a linear head -> aesthetic score. No training,
nothing fine-tuned here -- every number below comes from a public checkpoint.

Runs on the same tier as worker.py: compute-heavy, tolerates seconds of
latency, offloaded to a thread per call (see main.py's /analyze handler) so
it never blocks this node's asyncio loop -- gossip and raft both depend on
that loop staying responsive on a tight (50ms) schedule.

Models used (all downloaded on first use into MODEL_DIR, then cached --
nothing is committed to the repo, see the .gitignore entry for models/):

1. Saliency: cv2.saliency.StaticSaliencySpectralResidual -- classical CV,
   ships inside opencv-contrib-python, no download, no learned weights.

2 & 4. CLIP ViT-B/32, exported to ONNX (opset int8-quantized) by Xenova
   from openai/clip-vit-base-patch32:
   https://huggingface.co/Xenova/clip-vit-base-patch32
   (onnx/vision_model_quantized.onnx ~89MB, onnx/text_model_quantized.onnx
   ~65MB, tokenizer.json). Quantized rather than fp32 (~352MB+254MB) to
   keep the edge-node footprint reasonable -- still the same pretrained
   OpenAI CLIP weights, just int8.

4. Aesthetic score: LAION's "aesthetic-predictor" v1, the ViT-B/32 linear
   head (nn.Linear(512, 1), trained on Simulacra Aesthetic Captions):
   https://github.com/LAION-AI/aesthetic-predictor
   (sa_0_4_vit_b_32_linear.pth, ~3KB). This is the *original* aesthetic-
   predictor repo, not the more commonly cited "improved-aesthetic-
   predictor" -- that one only ships ViT-L/14 (768-dim) heads, incompatible
   with our ViT-B/32 (512-dim) embeddings. The .pth is PyTorch's zip
   checkpoint format; rather than pull in torch as a runtime dependency to
   read one Linear layer, _load_aesthetic_head() reads the two raw float32
   buffers straight out of the zip (shapes (1,512) weight + (1,) bias are
   fixed by the checkpoint's own definition, see the repo's README) --
   verified byte-for-byte equivalent to torch.load() during development.
"""

import io
import os
from functools import lru_cache

import cv2
import httpx
import numpy as np
from PIL import Image

MODEL_DIR = os.getenv("MODEL_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))
CLIP_BASE = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
AESTHETIC_URL = "https://raw.githubusercontent.com/LAION-AI/aesthetic-predictor/main/sa_0_4_vit_b_32_linear.pth"

CLIP_IMAGE_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_IMAGE_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
CLIP_IMAGE_SIZE = 224
CLIP_MAX_TOKENS = 77
CLIP_BOS_ID = 49406
CLIP_EOS_ID = 49407  # also used as the pad id, matching the HF tokenizer config

# One prompt per film stock the guest UI offers. Placeholder set -- swap in
# the real names/copy once the guest UI's actual film-stock list (item 4) is
# available; only the prompt text matters for matching quality, the dict
# keys are what /analyze returns as suggested_filter.
FILM_STOCKS = {
    "golden_hour":         "a photo with warm golden hour sunset lighting",
    "moody_bw":             "a moody black and white portrait photo",
    "vibrant_street":       "a vibrant, highly saturated street photography scene",
    "pastel_dreamy":        "a soft, pastel-toned, dreamy film photo",
    "high_contrast_noir":   "a high contrast, dramatic film noir photo",
    "vintage_warm":         "a warm vintage film photo with faded colors",
}


def _download(url: str, dest: str):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
    os.replace(tmp, dest)


def _cached(filename: str, url: str) -> str:
    path = os.path.join(MODEL_DIR, filename)
    if not os.path.exists(path):
        _download(url, path)
    return path


@lru_cache(maxsize=1)
def _vision_session():
    import onnxruntime as ort
    path = _cached("vision_model_quantized.onnx", f"{CLIP_BASE}/onnx/vision_model_quantized.onnx")
    return ort.InferenceSession(path, providers=["CPUExecutionProvider"])


@lru_cache(maxsize=1)
def _text_session():
    import onnxruntime as ort
    path = _cached("text_model_quantized.onnx", f"{CLIP_BASE}/onnx/text_model_quantized.onnx")
    return ort.InferenceSession(path, providers=["CPUExecutionProvider"])


@lru_cache(maxsize=1)
def _tokenizer():
    from tokenizers import Tokenizer
    path = _cached("tokenizer.json", f"{CLIP_BASE}/tokenizer.json")
    tok = Tokenizer.from_file(path)
    tok.enable_padding(length=CLIP_MAX_TOKENS, pad_id=CLIP_EOS_ID, pad_token="<|endoftext|>")
    tok.enable_truncation(max_length=CLIP_MAX_TOKENS)
    return tok


@lru_cache(maxsize=1)
def _aesthetic_head() -> tuple:
    """Returns (weight [1,512], bias [1]) as plain numpy arrays. See the
    module docstring for why this doesn't need torch."""
    import zipfile
    path = _cached("aesthetic_vit_b_32_linear.pth", AESTHETIC_URL)
    with zipfile.ZipFile(path) as z:
        weight = np.frombuffer(z.read("archive/data/0"), dtype=np.float32).reshape(1, 512).copy()
        bias = np.frombuffer(z.read("archive/data/1"), dtype=np.float32).copy()
    return weight, bias


@lru_cache(maxsize=1)
def _film_stock_text_embeddings() -> dict:
    """Text embeddings for the fixed FILM_STOCKS prompt set. Computed once
    (module-level cache) since the prompt set never changes per request."""
    sess = _text_session()
    tok = _tokenizer()
    out = {}
    for key, prompt in FILM_STOCKS.items():
        ids = np.array([tok.encode(prompt).ids], dtype=np.int64)
        emb = sess.run(["text_embeds"], {"input_ids": ids})[0][0]
        out[key] = emb / np.linalg.norm(emb)
    return out


def _preprocess_image(img: Image.Image) -> np.ndarray:
    img = img.convert("RGB")
    w, h = img.size
    scale = CLIP_IMAGE_SIZE / min(w, h)
    img = img.resize((round(w * scale), round(h * scale)), Image.BICUBIC)
    w, h = img.size
    left, top = (w - CLIP_IMAGE_SIZE) // 2, (h - CLIP_IMAGE_SIZE) // 2
    img = img.crop((left, top, left + CLIP_IMAGE_SIZE, top + CLIP_IMAGE_SIZE))
    arr = np.asarray(img).astype(np.float32) / 255.0
    arr = (arr - CLIP_IMAGE_MEAN) / CLIP_IMAGE_STD
    return arr.transpose(2, 0, 1)[None, ...].astype(np.float32)


def _embed_image(img: Image.Image) -> np.ndarray:
    pixel_values = _preprocess_image(img)
    emb = _vision_session().run(["image_embeds"], {"pixel_values": pixel_values})[0][0]
    return emb / np.linalg.norm(emb)


# ---- step 1+2: saliency -> subject centroid -> rule-of-thirds AR guide ----

WELL_COMPOSED_EPS = 0.03  # normalized fraction of width/height


def _saliency_centroid(bgr: np.ndarray) -> tuple[float, float]:
    sal = cv2.saliency.StaticSaliencySpectralResidual_create()
    ok, smap = sal.computeSaliency(bgr)
    smap = (smap * 255).astype(np.uint8)
    _, thresh = cv2.threshold(smap, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ys, xs = np.nonzero(thresh)
    if len(xs) == 0:
        h, w = smap.shape
        return w / 2, h / 2
    return float(xs.mean()), float(ys.mean())


def compute_ar_guide(img: Image.Image) -> dict:
    """Where the subject currently sits vs. the nearest rule-of-thirds
    intersection, and which way it needs to move within the frame to get
    there. dx/dy are normalized to [-1, 1] (fraction of width/height);
    move_subject_x/y describe the direction *the subject* should shift
    in-frame -- translating that into a camera-pan arrow (which is
    inverted relative to subject motion) is a frontend concern, not
    encoded here."""
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    cx, cy = _saliency_centroid(bgr)

    tx = min((w / 3, 2 * w / 3), key=lambda t: abs(t - cx))
    ty = min((h / 3, 2 * h / 3), key=lambda t: abs(t - cy))
    dx = (tx - cx) / w
    dy = (ty - cy) / h

    well_composed = abs(dx) < WELL_COMPOSED_EPS and abs(dy) < WELL_COMPOSED_EPS
    return {
        "centroid": {"x": round(cx, 1), "y": round(cy, 1)},
        "image_size": {"w": w, "h": h},
        "target": {"x": round(tx, 1), "y": round(ty, 1)},
        "dx": round(float(dx), 3),
        "dy": round(float(dy), 3),
        "move_subject_x": "centered" if abs(dx) < WELL_COMPOSED_EPS else ("right" if dx > 0 else "left"),
        "move_subject_y": "centered" if abs(dy) < WELL_COMPOSED_EPS else ("down" if dy > 0 else "up"),
        "well_composed": well_composed,
    }


# ---- step 3: CLIP filter suggestion ----

def suggest_filter(img: Image.Image) -> dict:
    image_emb = _embed_image(img)
    text_embs = _film_stock_text_embeddings()
    scores = {key: float(np.dot(image_emb, emb)) for key, emb in text_embs.items()}
    best = max(scores, key=scores.get)
    return {"suggested_filter": best, "scores": {k: round(v, 4) for k, v in scores.items()}}, image_emb


# ---- step 4: aesthetic score (reuses the CLIP embedding from step 3) ----

def score_aesthetic(image_emb: np.ndarray) -> float:
    weight, bias = _aesthetic_head()
    raw = float((weight @ image_emb + bias).reshape(-1)[0])
    return round(max(0.0, min(10.0, raw)), 2)


def analyze(image_bytes: bytes) -> dict:
    """Runs all four steps. Synchronous and CPU-bound by design -- callers
    (main.py) must offload this to a thread, not await it directly."""
    img = Image.open(io.BytesIO(image_bytes))
    img.load()

    ar_guide = compute_ar_guide(img)
    filter_result, image_emb = suggest_filter(img)
    aesthetic_score = score_aesthetic(image_emb)

    return {
        "ar_guide": ar_guide,
        "suggested_filter": filter_result["suggested_filter"],
        "filter_scores": filter_result["scores"],
        "aesthetic_score": aesthetic_score,
    }


def warmup():
    """Forces every model to download + load. Not called automatically on
    node startup (that would make every test that boots a node -- including
    ones that never touch /analyze -- pay a ~154MB download). Run it
    explicitly before a demo: `python -c "import ai_engine; ai_engine.warmup()"`."""
    _vision_session()
    _text_session()
    _tokenizer()
    _aesthetic_head()
    _film_stock_text_embeddings()
