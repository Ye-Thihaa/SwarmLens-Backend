"""Pretrained-only photo analysis: saliency -> AR reframe guide, CLIP ->
film-stock suggestion, CLIP + a linear head -> aesthetic score. No training,
nothing fine-tuned here -- every number below comes from a public checkpoint.

Two entry points, and the difference between them is persistence, not
computation. analyze() is the post-capture path: its aesthetic_score is
written to the replicated event log. preview() is the live-viewfinder
path used for guidance before the shutter (subject box, crop rectangle,
film-stock recommendation) -- it computes strictly more but writes
nothing, because it runs every couple of seconds against a frame nobody
has shot yet. See main.py's /analyze and /analyze/preview.

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

# One entry per film stock the guest UI actually offers. These keys are
# what /analyze returns as suggested_filter, and client-2's film strip
# (src/guest/data.ts) matches on them -- so the keys are a contract
# between the two, don't rename one side alone.
#
# `name` lives here rather than on the client because compose_reason()
# below builds one whole sentence mentioning the stock; splitting that
# sentence across the wire to keep the name client-side would be worse
# than the small duplication. The client owns presentation (`note`, strip
# order); this owns the canonical key, the CLIP prompt, and the copy.
#
# `prompts` describe the stock's *visual character*, not its name -- CLIP
# scores an image against what a photo taken on that stock looks like,
# and "Kodak Portra 400" as a bare string is a much weaker signal than a
# description of warm, soft, natural-skin-toned light.
#
# Several short prompts per stock rather than one long one, averaged in
# _film_stock_text_embeddings(). This is CLIP's own zero-shot recipe (the
# reference implementation ensembles 80 templates per class) and it's not
# cosmetic here: with one hand-written prompt each, a single stock won
# all 18 reference frames including near-black ones, because a long
# prompt full of generic photographic words picks up a constant offset
# that swamps the real per-image signal. Keep new prompts short and
# comparable in specificity for the same reason.
FILM_STOCKS = {
    "portra_400": {
        "name": "Portra 400",
        "prompts": [
            "a soft warm portrait photograph with natural skin tones",
            "a gentle muted color photograph of people in candlelight",
            "a low contrast portrait with creamy pastel colors",
        ],
        "why": "holds skin warm and the colors gentle",
    },
    "cinestill_800t": {
        "name": "Cinestill 800T",
        "prompts": [
            "a night photograph under tungsten street lights",
            "red halation glowing around bright lights at night",
            "a neon lit city street at night with cool blue shadows",
        ],
        "why": "lets the lights bloom the way tungsten night film does",
    },
    "tri_x_400": {
        "name": "Tri-X 400",
        "prompts": [
            "a black and white photograph",
            "a monochrome photograph with visible film grain",
            "a high contrast grayscale photograph with deep blacks",
        ],
        "why": "drops the color and leans on contrast and grain",
    },
    "ektar_100": {
        "name": "Ektar 100",
        "prompts": [
            "a bright daylight photograph with vivid saturated colors",
            "a sunny outdoor photograph with a deep blue sky",
            "a crisp colorful landscape in strong sunlight",
        ],
        "why": "pushes the saturation these colors are already reaching for",
    },
    "gold_200": {
        "name": "Gold 200",
        "prompts": [
            "a warm golden sunlit photograph",
            "a nostalgic faded film snapshot",
            "a photograph with warm amber tones at sunset",
        ],
        "why": "adds the warm, faded cast of an old sunlit snapshot",
    },
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
    """Text embeddings for the fixed FILM_STOCKS prompt set, mean-centered.
    Computed once (module-level cache) since the prompt set never changes.

    The centering matters more than it looks. Every prompt here starts
    from "a photograph of ...", so their embeddings share a large common
    component that has nothing to do with which stock suits a given image.
    Scoring against the raw embeddings, all six similarities land in a
    narrow 0.20-0.24 band and one stock wins nearly every real photo
    regardless of content -- measured, not assumed: three visibly
    different reference frames (a night motorcycle, a dappled-sun truck,
    a sunflower field) all returned the same stock. Subtracting the mean
    text embedding removes that shared component so the comparison runs
    on what actually differs between the prompts. Standard practice for
    zero-shot CLIP over a fixed, similarly-phrased label set."""
    sess = _text_session()
    tok = _tokenizer()
    raw = {}
    for key, stock in FILM_STOCKS.items():
        per_prompt = []
        for prompt in stock["prompts"]:
            ids = np.array([tok.encode(prompt).ids], dtype=np.int64)
            emb = sess.run(["text_embeds"], {"input_ids": ids})[0][0]
            per_prompt.append(emb / np.linalg.norm(emb))
        avg = np.mean(np.stack(per_prompt), axis=0)
        raw[key] = avg / np.linalg.norm(avg)

    mean = np.mean(np.stack(list(raw.values())), axis=0)
    return {k: (v - mean) / np.linalg.norm(v - mean) for k, v in raw.items()}


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


SUBJECT_TOP_PCT = 97.0      # keep only the top few % of saliency mass
SUBJECT_BLUR_DIV = 40.0     # blur sigma = max(w, h) / this
SUBJECT_BOX_SD = 1.2        # box half-width, in weighted standard deviations
SUBJECT_DIFFUSE_MAX = 0.30  # spread above this (fraction of the frame) = no subject


def _saliency_subject(bgr: np.ndarray) -> tuple[float, float, tuple[float, float, float, float], bool]:
    """One saliency pass -> (centroid_x, centroid_y, bbox, found).

    Position comes from the saliency-weighted centroid of the top
    `SUBJECT_TOP_PCT` of the blurred map, and extent from the weighted
    standard deviation about it. Both earlier approaches were measured
    and rejected:

    - Plain statistics over every above-Otsu pixel (min/max, or a
      6th/94th percentile) collapse to ~90% of the frame on any real
      photo, because foliage, gravel and fabric speckle saliency
      everywhere -- which silently made the reframe a permanent no-op.
    - Picking the strongest *connected component* fixes that but breaks
      differently: spectral residual responds to edges, not filled
      regions, so one object answers as several fragments (a uniform
      square splits into its left and right edges) and the winning
      fragment's centroid sits off to one side. Worse, including all the
      dimly-lit background drags the centroid toward the frame centre;
      measured on synthetic subjects at x=0.22 and x=0.72, the reported
      position landed at 0.40 and 0.53 -- both close enough to centre to
      cross the nearest rule-of-thirds line and invert the direction the
      guide told the user to move. Correct arithmetic, backwards advice.

    Keeping only the top few percent of saliency mass removes the
    background that caused that pull, and weighting by saliency means an
    object's several edge responses average back to the object. Measured
    against four synthetic subjects at known positions, centroid error is
    <= 1px in both axes.

    `found` is False when nothing crossed the threshold or when the mass
    is too spread out to be one subject. Callers must draw neither a
    subject box nor a crop rectangle then -- the fallback is a centred
    guess, not a detection.
    """
    h, w = bgr.shape[:2]
    fallback = (w / 2, h / 2, (w / 3, h / 3, 2 * w / 3, 2 * h / 3), False)

    sal = cv2.saliency.StaticSaliencySpectralResidual_create()
    ok, smap = sal.computeSaliency(bgr)
    if not ok:
        return fallback

    blur = cv2.GaussianBlur(
        (smap * 255).astype(np.uint8), (0, 0), sigmaX=max(w, h) / SUBJECT_BLUR_DIV
    ).astype(np.float32)

    thr = float(np.percentile(blur, SUBJECT_TOP_PCT))
    ys, xs = np.nonzero(blur >= thr)
    # Weight by height *above* the threshold, not absolute saliency, so the
    # cut-off pixels contribute ~0 instead of a uniform pedestal that would
    # pull the centroid back toward the middle of the selected region.
    wt = blur[ys, xs] - thr
    total = float(wt.sum())
    if total <= 0:
        return fallback

    cx = float((xs * wt).sum() / total)
    cy = float((ys * wt).sum() / total)
    sx = float(np.sqrt((wt * (xs - cx) ** 2).sum() / total))
    sy = float(np.sqrt((wt * (ys - cy) ** 2).sum() / total))

    if sx / w > SUBJECT_DIFFUSE_MAX or sy / h > SUBJECT_DIFFUSE_MAX:
        return (cx, cy, fallback[2], False)

    bbox = (
        max(0.0, cx - SUBJECT_BOX_SD * sx),
        max(0.0, cy - SUBJECT_BOX_SD * sy),
        min(float(w), cx + SUBJECT_BOX_SD * sx),
        min(float(h), cy + SUBJECT_BOX_SD * sy),
    )
    return cx, cy, bbox, True


def _saliency_centroid(bgr: np.ndarray) -> tuple[float, float]:
    cx, cy, _, _ = _saliency_subject(bgr)
    return cx, cy


_OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up", "centered": "centered"}


def _strength(mag: float) -> str:
    """Bucketed off the real magnitude, never a fixed adverb: dx/dy are
    fractions of frame width/height, so 0.04 genuinely is a nudge and 0.22
    genuinely is a big reframe. Copy that says "a little" regardless of
    the number would be invented flavor on top of measured data."""
    if mag < WELL_COMPOSED_EPS:
        return "none"
    if mag < 0.08:
        return "slight"
    return "moderate" if mag < 0.18 else "large"


def compute_ar_guide(img: Image.Image, subject: tuple | None = None) -> dict:
    """Where the subject currently sits vs. the nearest rule-of-thirds
    intersection, and which way it needs to move within the frame to get
    there. dx/dy are normalized to [-1, 1] (fraction of width/height).

    Two directions are reported, and they are opposites -- read the names
    carefully. move_subject_x/y is which way *the subject* should shift
    within the frame; pan_camera_x/y is which way *the camera* must turn
    to achieve that, which is inverted (to push the subject rightward in
    frame you pan left). Both are returned explicitly because a UI that
    picks the wrong one renders correct arithmetic as confidently wrong
    advice -- worse than no guidance at all, since it still looks
    trustworthy.

    Pass `subject` to reuse an existing _saliency_subject() result instead
    of paying for a second saliency pass."""
    if subject is None:
        bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
        subject = _saliency_subject(bgr)
    cx, cy, _bbox, found = subject
    w, h = img.size

    tx = min((w / 3, 2 * w / 3), key=lambda t: abs(t - cx))
    ty = min((h / 3, 2 * h / 3), key=lambda t: abs(t - cy))
    dx = (tx - cx) / w
    dy = (ty - cy) / h

    well_composed = abs(dx) < WELL_COMPOSED_EPS and abs(dy) < WELL_COMPOSED_EPS
    move_x = "centered" if abs(dx) < WELL_COMPOSED_EPS else ("right" if dx > 0 else "left")
    move_y = "centered" if abs(dy) < WELL_COMPOSED_EPS else ("down" if dy > 0 else "up")
    return {
        "centroid": {"x": round(cx, 1), "y": round(cy, 1)},
        "image_size": {"w": w, "h": h},
        "target": {"x": round(tx, 1), "y": round(ty, 1)},
        "dx": round(float(dx), 3),
        "dy": round(float(dy), 3),
        "move_subject_x": move_x,
        "move_subject_y": move_y,
        "pan_camera_x": _OPPOSITE[move_x],
        "pan_camera_y": _OPPOSITE[move_y],
        "strength": _strength(max(abs(dx), abs(dy))),
        "subject_found": found,
        "well_composed": well_composed,
    }


# ---- reframe: a crop rectangle + how much tighter it is ----

SUBJECT_SHARE = 0.38   # subject's long edge should fill this much of the crop
MAX_ZOOM = 4.0         # never recommend a crop tighter than this
MIN_USEFUL_ZOOM = 1.15  # below this the crop isn't worth telling anyone about


def compute_reframe(img: Image.Image, aspect: float | None = None,
                    subject: tuple | None = None) -> dict:
    """A concrete crop rectangle, not just a nudge direction: where the
    frame would sit if the subject were placed on a rule-of-thirds
    intersection and tightened to fill SUBJECT_SHARE of the frame.

    `aspect` is the crop's width/height -- pass the ratio the client is
    actually shooting (3:4, 1:1, ...) so the rectangle it draws matches
    the frame it will get. Defaults to the source image's own aspect.

    The rect is normalized to 0..1 of the source image so the caller can
    render it at any display size. `zoom` is relative to the largest crop
    of this same aspect that fits the frame -- i.e. how much tighter than
    the current framing -- so it's >= 1.0 by construction.

    `worth_it` is False when the subject wasn't really detected or the
    crop is barely tighter than what's already framed; the UI should show
    nothing at all rather than a rectangle that means nothing."""
    w, h = img.size
    if subject is None:
        bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
        subject = _saliency_subject(bgr)
    cx, cy, (sx0, sy0, sx1, sy1), found = subject
    aspect = aspect or (w / h)

    # Largest crop of this aspect that still fits inside the frame.
    max_w = min(float(w), h * aspect)
    max_h = max_w / aspect

    sw, sh = max(sx1 - sx0, 1.0), max(sy1 - sy0, 1.0)
    crop_w = max(sw / SUBJECT_SHARE, (sh / SUBJECT_SHARE) * aspect)
    crop_w = min(crop_w, max_w)                 # never larger than fits
    crop_w = max(crop_w, max_w / MAX_ZOOM)      # never tighter than MAX_ZOOM
    crop_h = crop_w / aspect

    # Put the subject on the nearer thirds line of the *crop*. Choosing the
    # near side (rather than always 1/3) is what keeps the rectangle from
    # immediately running off the edge it's closest to.
    third_x = 1 / 3 if cx < w / 2 else 2 / 3
    third_y = 1 / 3 if cy < h / 2 else 2 / 3
    x0 = min(max(cx - third_x * crop_w, 0.0), w - crop_w)
    y0 = min(max(cy - third_y * crop_h, 0.0), h - crop_h)

    zoom = max_w / crop_w
    return {
        "rect": {
            "x": round(x0 / w, 4), "y": round(y0 / h, 4),
            "w": round(crop_w / w, 4), "h": round(crop_h / h, 4),
        },
        "subject_box": {
            "x": round(sx0 / w, 4), "y": round(sy0 / h, 4),
            "w": round((sx1 - sx0) / w, 4), "h": round((sy1 - sy0) / h, 4),
        },
        "zoom": round(float(zoom), 2),
        "worth_it": bool(found and zoom >= MIN_USEFUL_ZOOM),
        "subject_found": found,
    }


# ---- scene colour, for the recommendation sentence ----

# OpenCV hue is 0-179, and red wraps around both ends of that range.
_HUE_BANDS = [
    (0, 10, "red"), (10, 22, "orange"), (22, 33, "yellow"), (33, 78, "green"),
    (78, 100, "cyan"), (100, 130, "blue"), (130, 157, "purple"), (157, 180, "red"),
]


def describe_scene(img: Image.Image) -> dict:
    """Dominant hues + saturation/brightness, straight from an HSV
    histogram. Cheap enough to run on every preview frame, and it's what
    lets the recommendation sentence say something specific about *this*
    scene instead of a generic line about the film stock.

    Very dark pixels are excluded before the hue histogram: their hue is
    numerically defined but perceptually meaningless, and including them
    makes every night shot report whatever noise tinted the shadows.
    Bright pixels are NOT excluded -- an earlier version dropped V > 250
    as blown highlights, which silently discarded *every* pixel of a
    fully-saturated image (a vivid orange is V=255 with plenty of hue)
    and reported a bright sunset as colorless. Achromatic pixels are
    already handled by the saturation gate below, which is the correct
    test for "this pixel has no useful hue"."""
    small = img.convert("RGB")
    small.thumbnail((160, 160), Image.BILINEAR)
    hsv = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]

    lit = val > 35
    colored = lit & (sat > 55)
    total = int(colored.sum())

    colors: list[str] = []
    if total > 0:
        counts = np.bincount(hue[colored].ravel(), minlength=180)
        shares: dict[str, float] = {}
        for lo, hi, name in _HUE_BANDS:
            shares[name] = shares.get(name, 0.0) + float(counts[lo:hi].sum()) / total
        colors = [n for n, s in sorted(shares.items(), key=lambda kv: -kv[1]) if s >= 0.15][:2]

    lit_px = int(lit.sum()) or 1
    mean_sat = float(sat[lit].mean()) if lit.any() else 0.0
    mean_val = float(val[lit].mean()) if lit.any() else 0.0
    color_share = total / lit_px

    return {
        "colors": colors,
        "saturation": "high" if (mean_sat > 110 and color_share > 0.35)
                      else ("low" if mean_sat < 55 else "medium"),
        "brightness": "dark" if mean_val < 70 else ("bright" if mean_val > 175 else "even"),
        "mean_saturation": round(mean_sat, 1),
        "mean_brightness": round(mean_val, 1),
    }


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{words[0]} and {words[1]}"


def compose_reason(scene: dict, stock_key: str, confident: bool = True) -> str:
    """The one-line 'why this stock' shown over the viewfinder. Templated
    from measured values (dominant hues, saturation, brightness) plus the
    stock's own `why` clause -- deliberately not an LLM call: it would be
    a new runtime dependency and a network round-trip for a sentence that
    has maybe a dozen honest shapes, and this module's whole contract is
    pretrained-only with nothing generated out of thin air.

    `confident` comes from CONFIDENT_MARGIN. When the top two stocks are
    effectively tied, the sentence says so instead of asserting a
    preference the scores don't support -- the scene half is still a real
    measurement, so it's reported either way."""
    stock = FILM_STOCKS[stock_key]
    colors, sat, bright = scene["colors"], scene["saturation"], scene["brightness"]

    if colors and sat == "high":
        lead = f"Vivid {_join(colors)}"
    elif colors:
        lead = f"Mostly {_join(colors)}"
    elif sat == "low" and bright == "dark":
        lead = "Dim, near-colorless light"
    elif sat == "low":
        lead = "Flat, muted color"
    else:
        lead = "Even, unremarkable color"

    if bright == "dark" and colors:
        lead += ", low light"

    if not confident:
        return f"{lead} — no strong preference here, {stock['name']} is the closest."
    return f"{lead} — {stock['name']} {stock['why']}."


# ---- step 3: CLIP filter suggestion ----

# Margin (top score minus runner-up) below which the two are a tie in all
# but name. Measured, not guessed: across the reference frames, genuine
# picks separate by 0.01-0.06 while coin-flips sit under 0.005. The UI
# must not render a 0.0009 margin in the same confident voice as a 0.05
# one -- see compose_reason().
CONFIDENT_MARGIN = 0.01


def suggest_filter(img: Image.Image) -> dict:
    image_emb = _embed_image(img)
    text_embs = _film_stock_text_embeddings()
    scores = {key: float(np.dot(image_emb, emb)) for key, emb in text_embs.items()}
    best = max(scores, key=scores.get)
    return {"suggested_filter": best, "scores": {k: round(v, 5) for k, v in scores.items()}}, image_emb


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


def preview(image_bytes: bytes, aspect: float | None = None) -> dict:
    """Live-viewfinder analysis, for guidance *before* the shutter.

    Same pretrained models and the same saliency/CLIP work as analyze(),
    plus a crop rectangle (compute_reframe) and a recommendation sentence
    (compose_reason). The difference that matters is what the caller does
    with it: analyze() results get written to the replicated event log as
    an aesthetic_score, and this one's must not be. A viewfinder calls
    this every couple of seconds against a photo_id that doesn't exist
    yet -- persisting that would flood the log, gossip it to every node,
    and skew /zones' avg_aesthetic with scores for frames nobody shot.
    See main.py's /analyze/preview.

    One saliency pass is shared between the AR guide and the reframe;
    one CLIP image embedding is shared between the filter picks and the
    aesthetic score. Still synchronous and CPU-bound -- offload it."""
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    bgr = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    subject = _saliency_subject(bgr)

    ar_guide = compute_ar_guide(img, subject=subject)
    reframe = compute_reframe(img, aspect=aspect, subject=subject)
    scene = describe_scene(img)
    filter_result, image_emb = suggest_filter(img)

    scores = filter_result["scores"]
    ranked = sorted(scores, key=scores.get, reverse=True)
    best = filter_result["suggested_filter"]
    margin = scores[ranked[0]] - scores[ranked[1]]
    confident = margin >= CONFIDENT_MARGIN

    return {
        "ar_guide": ar_guide,
        "reframe": reframe,
        "scene": scene,
        "suggested_filter": best,
        "confident": confident,
        "margin": round(float(margin), 5),
        "picks": [
            {"key": k, "name": FILM_STOCKS[k]["name"], "score": scores[k]}
            for k in ranked[:3]
        ],
        "reason": compose_reason(scene, best, confident),
        "aesthetic_score": score_aesthetic(image_emb),
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
