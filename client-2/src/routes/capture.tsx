import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { filmStocks } from "@/guest/data";
import { GuestTabs } from "@/guest/ui";
import {
  pickNode,
  postAnalyzePreview,
  prettyZone,
  ZONES,
  type ComposePreview,
  type ZoneScore,
  getZones,
} from "@/lib/api";
import { currentGuestId, tick } from "@/lib/guest";
import { addPhoto, syncOutbox, useOutbox } from "@/lib/outbox";

export const Route = createFileRoute("/capture")({
  head: () => ({
    meta: [
      { title: "Camera — SwarmLens" },
      {
        name: "description",
        content:
          "A retro point-and-shoot: film stocks, ISO and EV dials, flash, self-timer, date stamp. Shoot offline; frames wait on the roll until the room is reachable.",
      },
      { property: "og:title", content: "Camera — SwarmLens" },
      { property: "og:description", content: "A camera that has an instinct about the room." },
    ],
  }),
  component: Capture,
});

const ISO = [100, 200, 400, 800, 1600, 3200];
const SHUTTER = ["1/500", "1/250", "1/125", "1/60", "1/30", "1/15"];
const EV = ["-1.0", "-0.7", "-0.3", "0.0", "+0.3", "+0.7", "+1.0"];
// `ar` is width/height and must match `cls` exactly: it's what gets sent to
// /analyze/preview so the crop rectangle that comes back is expressed in
// the same frame the guest is actually looking at.
const RATIOS = [
  { key: "3:4", cls: "aspect-[3/4]", ar: 3 / 4 },
  { key: "1:1", cls: "aspect-square", ar: 1 },
  { key: "strip", cls: "aspect-[1/3]", ar: 1 / 3 },
] as const;

/** Per-stock starting point for the review sheet's TONE/COLOR/PALETTE
 * sliders -- picking a different stock on the strip resets to its own
 * preset rather than keeping whatever the previous stock's sliders were
 * left at, matching a real film swap more than a shared global edit. Keyed
 * by the same filmStocks[].key contract ai_engine.py's FILM_STOCKS uses. */
const STOCK_PRESETS: Record<string, { tone: number; color: number; palette: number }> = {
  portra_400: { tone: -10, color: 85, palette: 65 },
  cinestill_800t: { tone: -20, color: 100, palette: 80 },
  tri_x_400: { tone: 0, color: 0, palette: 100 },
  ektar_100: { tone: 10, color: 130, palette: 60 },
  gold_200: { tone: 5, color: 90, palette: 70 },
};
const DEFAULT_PRESET = { tone: 0, color: 100, palette: 50 };

/** Discrete stops for the manual ZOOM dial in the settings panel -- deliberately
 * separate from the AI's tap-to-zoom, which can recommend an off-center rect.
 * The manual dial always produces a centered crop (see centeredZoomRect) so
 * "zoom out a bit" from wherever you are has one unambiguous meaning instead
 * of needing to reconstruct what an earlier AI tap was centered on. */
const ZOOM_STEPS = [1, 1.3, 1.6, 2, 2.5, 3, 4] as const;

/** How often the viewfinder asks a node to look at what it's seeing. One
 * analysis measured ~37ms server-side, so this is set by taste and by
 * politeness to a shared node rather than by compute: fast enough to feel
 * live, slow enough that several guests on one node don't spend their
 * whole time being shed with 503s. */
const PREVIEW_INTERVAL_MS = 1500;
/** CLIP resizes its input to 224px anyway; sending more is pure upload
 * cost on a phone's uplink for zero extra signal. */
const PREVIEW_EDGE_PX = 224;

/** A knurled dial: tap the ticks to step through discrete values, like a real camera. */
function Dial({
  label,
  values,
  index,
  onChange,
}: {
  label: string;
  values: readonly (string | number)[];
  index: number;
  onChange: (i: number) => void;
}) {
  return (
    <div className="min-w-0 flex-1">
      <p className="font-mono text-[0.52rem] tracking-[0.18em] text-fixer-dim">{label}</p>
      <div className="mt-1 flex items-stretch overflow-hidden rounded-sm border border-fixer/25 bg-emulsion/70">
        <button
          onClick={() => onChange(Math.max(0, index - 1))}
          aria-label={`${label} down`}
          className="px-1.5 font-mono text-[0.7rem] text-fixer-dim active:bg-fixer/10"
        >
          −
        </button>
        <span className="flex-1 border-x border-fixer/15 py-1 text-center font-mono text-[0.7rem] font-bold text-drifting">
          {values[index]}
        </span>
        <button
          onClick={() => onChange(Math.min(values.length - 1, index + 1))}
          aria-label={`${label} up`}
          className="px-1.5 font-mono text-[0.7rem] text-fixer-dim active:bg-fixer/10"
        >
          +
        </button>
      </div>
    </div>
  );
}

function Toggle({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={on}
      className={`rounded-sm border px-2 py-1.5 font-mono text-[0.55rem] tracking-[0.14em] transition ${
        on
          ? "border-drifting bg-drifting/20 text-drifting"
          : "border-fixer/20 bg-emulsion/60 text-fixer-dim"
      }`}
    >
      {children}
    </button>
  );
}

/** A continuous adjust knob for the review sheet -- unlike Dial's discrete
 * ticks, TONE/COLOR/PALETTE are free-ranging canvas filter inputs, so a
 * native range input (cheap, correct touch/keyboard handling for free)
 * fits better than reimplementing drag math. */
function Slider({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
}) {
  return (
    <div>
      <div className="flex items-center justify-between">
        <p className="font-mono text-[0.5rem] tracking-[0.16em] text-fixer-dim">{label}</p>
        <p className="font-mono text-[0.6rem] font-bold text-drifting">
          {value > 0 ? `+${value}` : value}
        </p>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 w-full accent-drifting"
      />
    </div>
  );
}

function Capture() {
  const videoRef = useRef<HTMLVideoElement>(null);
  // A capture (captureFrame) makes its own throwaway canvas rather than
  // reusing a ref, precisely so it can never collide with this one: the
  // preview loop and a shutter press can land in the same tick, and
  // sharing a canvas would let a capture and an analysis scribble over
  // each other's pixels mid-draw.
  const previewCanvasRef = useRef<HTMLCanvasElement>(null);
  const [cameraError, setCameraError] = useState<string | null>(null);

  // AI compose: live guidance from POST /analyze/preview (ai_engine.py's
  // saliency + CLIP, same models /analyze uses after a capture -- this path
  // just never persists anything).
  const [aiOn, setAiOn] = useState(true);
  const [ai, setAi] = useState<ComposePreview | null>(null);
  const [aiScanning, setAiScanning] = useState(false);
  const stockTouched = useRef(false);

  // Which way the camera faces. "user" (selfie) is shown mirrored, the way
  // every phone camera does it -- which has consequences for the AI overlay,
  // see grabPreviewFrame and panInstruction.
  const [facing, setFacing] = useState<"environment" | "user">("environment");
  const mirrored = facing === "user";

  // Scattered once per mount, not per render -- the scan overlay flickers
  // in and out with every "reading the light" phase, and regenerating
  // positions on each of those renders would make the particles jump
  // around instead of twinkling in place.
  const scanDots = useMemo(
    () =>
      Array.from({ length: 26 }, () => ({
        left: Math.random() * 100,
        top: Math.random() * 100,
        size: Math.random() < 0.25 ? 3 : 1.5,
        delay: Math.random() * 2.2,
        duration: 1.4 + Math.random() * 1.6,
      })),
    [],
  );

  // Auto-zoom: when on (the default), the viewfinder zooms itself into
  // whatever crop ai_engine.py's saliency-based subject detection
  // recommends, every tick -- no tap required. `rect` is always absolute
  // against the ar-cropped full frame, in the SAME coordinate space
  // grabPreviewFrame already mirrors into when facing the selfie camera, so
  // shootOnce can crop straight from it without caring which camera is
  // active. Read via digitalZoomRef (below) inside the preview tick, NOT as
  // a dependency of that effect -- see the ref's own comment for why.
  const [digitalZoom, setDigitalZoom] = useState<{
    rect: { x: number; y: number; w: number; h: number };
    zoom: number;
  } | null>(null);
  const digitalZoomRef = useRef(digitalZoom);
  useEffect(() => {
    digitalZoomRef.current = digitalZoom;
  }, [digitalZoom]);

  // Touching the manual ZOOM dial (or the reset chip) takes over from
  // auto-zoom; the AUTO ZOOM toggle in settings hands control back. Kept as
  // a ref, read inside the same tick that would otherwise auto-apply a new
  // rect, so flipping it takes effect on the very next frame rather than
  // waiting for the polling effect to notice a stale closure.
  const [autoZoom, setAutoZoom] = useState(true);
  const autoZoomRef = useRef(autoZoom);
  useEffect(() => {
    autoZoomRef.current = autoZoom;
  }, [autoZoom]);

  // The manual ZOOM dial's own position -- kept in sync with digitalZoom
  // (see the auto-zoom tick and the reset effect below) so the settings
  // panel never shows a zoom level that disagrees with what's on screen.
  const [zoomIndex, setZoomIndex] = useState(0);

  // Auto-zoom stability (see the tick effect for the two-rule shape:
  // engaging from nothing is immediate, only CHANGING or RELEASING an
  // already-applied zoom is debounced). RECT_JITTER_TOL has to be loose
  // enough to absorb ordinary tick-to-tick sensor noise -- autofocus
  // hunting, exposure settling, JPEG artifacts -- or two consecutive real
  // reads essentially never agree and auto-zoom silently never commits
  // anything, which is exactly what a first, stricter version of this did.
  const ZOOM_STABLE_TICKS = 2;
  const RECT_JITTER_TOL = 0.15;
  const zoomCandidateRef = useRef<{ rect: { x: number; y: number; w: number; h: number }; zoom: number } | null>(
    null,
  );
  const zoomStableCountRef = useRef(0);

  const [stock, setStock] = useState(0);

  // Post-capture review: the shutter no longer queues straight to the
  // outbox for a single (non-burst) frame. It freezes the shot on its own
  // canvas -- decoupled from canvasRef/videoRef, which keep going -- and
  // holds it here until RETAKE (discarded) or USE THIS FRAME (baked
  // through buildFilterCss and only then queued). Burst mode skips this
  // entirely: reviewing three frames one at a time is a different, un-asked-
  // for feature, so it keeps the old immediate-queue path.
  const [reviewing, setReviewing] = useState<{
    canvas: HTMLCanvasElement;
    previewUrl: string;
    stock: number;
    tone: number;
    color: number;
    palette: number;
  } | null>(null);

  const [zone, setZone] = useState(ZONES[0]!);
  const [reachable, setReachable] = useState(false);
  const [composing, setComposing] = useState(false);
  const [topZone, setTopZone] = useState<ZoneScore | null>(null);
  const composeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // camera settings — the "doca cam" panel (all cosmetic, local to this phone)
  const [iso, setIso] = useState(2);
  const [shutter, setShutter] = useState(3);
  const [ev, setEv] = useState(4);
  const [ratio, setRatio] = useState(0);
  const [flash, setFlash] = useState(false);
  const [grid, setGrid] = useState(true);
  const [stamp, setStamp] = useState(true);
  const [timer, setTimer] = useState(0); // 0 | 3 | 10
  const [panel, setPanel] = useState(false);
  const [burst, setBurst] = useState(false);
  const [count, setCount] = useState<number | null>(null);

  const outbox = useOutbox();
  const myPhotos = useMemo(() => outbox.filter((o) => o.kind === "photo"), [outbox]);
  const queued = myPhotos.filter((o) => !o.synced).length;
  const frame = myPhotos.length;

  const showNudge = !composing && !panel;

  // The zoom rect's coordinate space is tied to both the frame ratio and
  // which camera is active (mirrored or not -- see grabPreviewFrame). Either
  // changing invalidates it. Turning AI off removes the only control that
  // can drive it, so treat that as a reset too rather than leaving the guest
  // stuck zoomed in with no way back. Also hands control back to auto-zoom,
  // matching "changed something major, start fresh" rather than silently
  // staying in whatever manual/auto state was active before.
  useEffect(() => {
    setDigitalZoom(null);
    setZoomIndex(0);
    setAutoZoom(true);
    zoomCandidateRef.current = null;
    zoomStableCountRef.current = 0;
  }, [ratio, aiOn, facing]);

  /** Drives digitalZoom from the manual ZOOM dial: a plain centered crop at
   * the selected step. Touching this dial is a deliberate "I'll drive"
   * action, so it takes over from auto-zoom rather than being overwritten
   * by the very next tick's recommendation. */
  function onManualZoomChange(i: number) {
    setAutoZoom(false);
    setZoomIndex(i);
    const z = ZOOM_STEPS[i]!;
    setDigitalZoom(z <= 1 ? null : { rect: centeredZoomRect(z), zoom: z });
  }

  // real camera feed, not a bundled photo. Re-runs on flip; the cleanup
  // stops the old tracks first, since a phone won't hand out the front
  // camera while the back one still holds the hardware.
  useEffect(() => {
    let stream: MediaStream | null = null;
    let cancelled = false;
    setCameraError(null);
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: facing }, audio: false })
      .then((s) => {
        if (cancelled) {
          s.getTracks().forEach((t) => t.stop());
          return;
        }
        stream = s;
        if (videoRef.current) videoRef.current.srcObject = s;
      })
      .catch((e) => {
        if (!cancelled) setCameraError(String(e));
      });
    return () => {
      cancelled = true;
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, [facing]);

  // real reachability, polled the same way the guest roll and gallery do
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const node = await pickNode();
      if (!cancelled) setReachable(node !== null);
    }
    void poll();
    const id = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // the live cue from the venue heatmap: real top-scoring zone, polled
  // softly. Disappears the instant the guest picks a zone that's already
  // the leader, or has no opinion to offer yet (fewer than 2 photos).
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const node = await pickNode();
      if (!node || cancelled) return;
      try {
        const zones = await getZones(node);
        const ranked = zones
          .filter((z) => z.photos >= 2)
          .sort((a, b) => b.likes * 2 + b.photos - (a.likes * 2 + a.photos));
        if (!cancelled) setTopZone(ranked[0] ?? null);
      } catch {
        // stay on the last known-good read
      }
    }
    void poll();
    const id = setInterval(poll, 5000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // Live composition guidance, one analysis at a time (recursive timeout, not
  // setInterval, so a slow node can never stack up overlapping requests).
  //
  // Deliberately degrades to silence rather than to a stale opinion: when no
  // node answers, the overlay clears and the camera carries on exactly as it
  // did before this feature existed. Guidance is the only part of this screen
  // that needs the cluster -- capture, the outbox and the roll are all local,
  // so a partition costs the guest advice, never a photo.
  const isReviewing = reviewing !== null;

  useEffect(() => {
    // Reading isReviewing (a boolean), not reviewing itself: reviewing is a
    // new object on every slider drag, and depending on the object would
    // restart this whole polling loop -- aborting an in-flight request and
    // rescheduling -- on every tick of a slider the guest is dragging.
    if (!aiOn || cameraError || isReviewing) {
      setAi(null);
      setAiScanning(false);
      return;
    }
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const controller = new AbortController();
    const ar = RATIOS[ratio]!.ar;

    async function tick() {
      if (stopped) return;
      const video = videoRef.current;
      const canvas = previewCanvasRef.current;
      const node = video && canvas && video.videoWidth > 0 ? await pickNode() : null;

      if (!stopped && node) {
        // In auto-zoom mode, always analyze the FULL frame, not whatever
        // digitalZoom currently holds: each tick proposes a fresh absolute
        // crop, so a subject that moves -- or stops needing as tight a
        // crop -- is tracked continuously instead of ratcheting in via
        // composed rects and never loosening back up. In manual mode
        // (the guest is driving the ZOOM dial), analyze whatever's
        // actually on screen instead, so the pan arrows react to what the
        // guest is really looking at. Mirroring happens inside
        // grabPreviewFrame either way, and must -- the zoom rect is
        // expressed in mirrored space when facing the selfie camera, so
        // the two have to agree on orientation or every box lands on the
        // wrong side of its subject.
        const zoomRectForAnalysis = autoZoomRef.current ? undefined : digitalZoomRef.current?.rect;
        const frame = grabPreviewFrame(video!, canvas!, ar, mirrored, zoomRectForAnalysis);
        if (frame) {
          setAiScanning(true);
          try {
            const result = await postAnalyzePreview(
              node,
              { image_base64: frame, aspect: ar },
              controller.signal,
            );
            // null means the node shed this frame with 503 (all preview
            // slots busy). Keep the previous reading on screen rather than
            // blanking for one tick -- it's one interval stale, not wrong.
            if (!stopped && result) {
              setAi(result);
              // Never override a deliberate choice, and never auto-apply on
              // a coin flip: only load the recommendation while the guest
              // hasn't touched the strip and the scores actually separate.
              if (!stockTouched.current && result.confident) {
                const i = filmStocks.findIndex((f) => f.key === result.suggested_filter);
                if (i >= 0) setStock(i);
              }
              // Real auto-zoom. Absolute against the full frame (see
              // above), so a committed rect replaces digitalZoom wholesale
              // rather than composing with whatever was there -- a subject
              // walking closer loosens the crop just as readily as a
              // distant one tightens it.
              //
              // Two different rules for two different moments, not one
              // stability gate for both: engaging FROM NOTHING applies
              // immediately -- any delay there just reads as "auto-zoom
              // isn't working". CHANGING or RELEASING an already-applied
              // zoom is what needs debouncing, since that's what flickers
              // when consecutive reads disagree by sensor noise alone
              // (autofocus hunting, exposure settling, JPEG artifacts --
              // real cameras are noisier tick to tick than a static test
              // image). RECT_JITTER_TOL is deliberately generous: it only
              // needs to catch "basically the same framing", not exact
              // agreement, or ordinary noise defeats it exactly like the
              // stricter version did (this shipped once already and simply
              // never committed on a real phone).
              if (autoZoomRef.current) {
                const candidate = result.reframe.worth_it
                  ? { rect: result.reframe.rect, zoom: result.reframe.zoom }
                  : null;
                const current = digitalZoomRef.current;

                if (candidate && current && rectsSimilar(candidate.rect, current.rect, RECT_JITTER_TOL)) {
                  // Same framing as what's already applied -- nothing to do,
                  // and this is also the natural point to stop counting
                  // toward some earlier, now-stale pending change.
                  zoomCandidateRef.current = null;
                  zoomStableCountRef.current = 0;
                } else if (current === null) {
                  // Nothing applied yet: engage on the very first worth_it
                  // reading, no debounce. A guest should see the zoom react
                  // as soon as they point at something, not after a pause.
                  if (candidate) {
                    setDigitalZoom(candidate);
                    setZoomIndex(nearestZoomStep(candidate.zoom));
                  }
                  zoomCandidateRef.current = null;
                  zoomStableCountRef.current = 0;
                } else {
                  // Something's already applied and this reading disagrees
                  // with it (a real change, or the subject's gone) -- only
                  // commit once the SAME alternative has been read
                  // ZOOM_STABLE_TICKS times in a row, so a guest actively
                  // panning or tilting never gets fought with mid-move.
                  const prevPending = zoomCandidateRef.current;
                  const pendingAgrees =
                    (candidate === null && prevPending === null) ||
                    (candidate !== null &&
                      prevPending !== null &&
                      rectsSimilar(candidate.rect, prevPending.rect, RECT_JITTER_TOL));
                  zoomStableCountRef.current = pendingAgrees ? zoomStableCountRef.current + 1 : 1;
                  zoomCandidateRef.current = candidate;

                  if (zoomStableCountRef.current >= ZOOM_STABLE_TICKS) {
                    if (candidate) {
                      setDigitalZoom(candidate);
                      setZoomIndex(nearestZoomStep(candidate.zoom));
                    } else {
                      setDigitalZoom(null);
                      setZoomIndex(0);
                    }
                    zoomCandidateRef.current = null;
                    zoomStableCountRef.current = 0;
                  }
                }
              }
            }
          } catch {
            if (!stopped) setAi(null); // aborted, or the node went away
          } finally {
            if (!stopped) setAiScanning(false);
          }
        }
      } else if (!stopped) {
        setAi(null);
      }
      if (!stopped) timer = setTimeout(tick, PREVIEW_INTERVAL_MS);
    }

    void tick();
    return () => {
      stopped = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
    // digitalZoom is read via a ref (digitalZoomRef), NOT listed as a dep
    // here: auto-zoom sets it on nearly every tick, and depending on it
    // directly would abort and restart this whole polling loop -- and its
    // in-flight request -- every single time, faster than any request
    // could ever complete. autoZoom itself IS a dep: it only changes via a
    // deliberate tap (the dial, the reset chip, the settings toggle), so
    // restarting the loop there is rare and gives an immediate re-read
    // instead of waiting up to PREVIEW_INTERVAL_MS for the next tick.
  }, [aiOn, cameraError, ratio, mirrored, isReviewing, autoZoom]);

  useEffect(() => {
    return () => {
      if (composeTimer.current) clearTimeout(composeTimer.current);
    };
  }, []);

  useEffect(() => {
    if (count === null) return;
    if (count === 0) {
      setCount(null);
      shoot();
      return;
    }
    const t = setTimeout(() => setCount((c) => (c === null ? null : c - 1)), 1000);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [count]);

  function beginComposing() {
    setComposing(true);
    if (composeTimer.current) clearTimeout(composeTimer.current);
    composeTimer.current = setTimeout(() => setComposing(false), 6000);
  }

  /** Grabs the current video frame, bakes in the currently selected stock's
   * default look (same buildFilterCss the review sheet and confirmReview
   * use, just at STOCK_PRESETS defaults since there's no per-shot review to
   * adjust from), and queues it in the offline outbox immediately -- the
   * shutter never waits on the network. Used for burst mode, which bypasses
   * the review sheet entirely (reviewing three frames one at a time is a
   * separate feature nobody asked for) but should still come out looking
   * like the stock the guest picked, not a flat unfiltered frame -- a burst
   * shot skipping the effect entirely was a real gap, not a deliberate
   * simplification. */
  function shootOnce() {
    const raw = captureFrame(videoRef.current, digitalZoom?.rect, RATIOS[ratio]!.ar, mirrored);
    if (!raw) return;
    const stockKey = filmStocks[stock]!.key;
    const preset = STOCK_PRESETS[stockKey] ?? DEFAULT_PRESET;
    const canvas = document.createElement("canvas");
    canvas.width = raw.width;
    canvas.height = raw.height;
    const ctx = canvas.getContext("2d")!;
    ctx.filter = buildFilterCss(stockKey, preset.tone, preset.color, preset.palette);
    ctx.drawImage(raw, 0, 0);
    canvas.toBlob(
      async (blob) => {
        if (!blob) return;
        const image_base64 = await blobToBase64(blob);
        addPhoto({
          local_id: crypto.randomUUID(),
          guest_id: currentGuestId(),
          zone,
          composition_score: 0,
          vclock: tick(),
          image_base64,
        });
        void syncOutbox();
      },
      "image/jpeg",
      0.85,
    );
  }

  /** A single (non-burst) shot freezes onto its own canvas -- decoupled
   * from videoRef, which keeps playing underneath -- and opens the review
   * sheet instead of queuing immediately. Preset TONE/COLOR/PALETTE come
   * from whichever stock is currently selected (the live AI pick, if the
   * guest hasn't touched the strip), so the review opens already looking
   * like a reasonable default rather than flat/neutral. */
  function shoot() {
    if (burst) {
      for (let i = 0; i < 3; i++) shootOnce();
      return;
    }
    const canvas = captureFrame(videoRef.current, digitalZoom?.rect, RATIOS[ratio]!.ar, mirrored);
    if (!canvas) return;
    const preset = STOCK_PRESETS[filmStocks[stock]!.key] ?? DEFAULT_PRESET;
    setReviewing({
      canvas,
      previewUrl: canvas.toDataURL("image/jpeg", 0.85),
      stock,
      ...preset,
    });
  }

  function onShutter() {
    beginComposing();
    if (timer > 0) setCount(timer);
    else shoot();
  }

  /** RETAKE: discard the frozen frame, back to the live viewfinder. */
  function retakeReview() {
    setReviewing(null);
  }

  /** USE THIS FRAME: bakes TONE/COLOR/PALETTE into real pixels via
   * ctx.filter (not just a CSS preview that vanishes on save) and only
   * then queues it -- same offline-first outbox path as a normal shot. */
  function confirmReview() {
    if (!reviewing) return;
    const filterCss = buildFilterCss(
      filmStocks[reviewing.stock]!.key,
      reviewing.tone,
      reviewing.color,
      reviewing.palette,
    );
    const out = document.createElement("canvas");
    out.width = reviewing.canvas.width;
    out.height = reviewing.canvas.height;
    const ctx = out.getContext("2d")!;
    ctx.filter = filterCss;
    ctx.drawImage(reviewing.canvas, 0, 0);
    out.toBlob(
      async (blob) => {
        if (!blob) return;
        const image_base64 = await blobToBase64(blob);
        addPhoto({
          local_id: crypto.randomUUID(),
          guest_id: currentGuestId(),
          zone,
          composition_score: 0,
          vclock: tick(),
          image_base64,
        });
        void syncOutbox();
      },
      "image/jpeg",
      0.9,
    );
    // Carry the review's stock choice back to the live strip so the next
    // shot starts from what was just confirmed, and stop the AI pick from
    // silently overriding a choice the guest just acted on.
    stockTouched.current = true;
    setStock(reviewing.stock);
    setReviewing(null);
  }

  const today = "26 07 08";

  // Visually zoom the video to `digitalZoom.rect` by scaling it up around
  // that rect's center, then translating the (now off-center) rect back to
  // the middle of the box. Derived once and worth writing down: with
  // transform-origin left at its 50%/50% default, `scale(S) translate(tx,ty)`
  // resolves (percentages are relative to the element's own unscaled box) to
  // screen = 0.5 + S * ((u + tx) - 0.5) for a source fraction u -- solving
  // that for "screen == (u - rect.x) / rect.w" gives S = 1/rect.w and
  // tx = 0.5 - (rect.x + rect.w/2), independent of u, i.e. a single
  // translate works for the whole rect, not just its center.
  // Composed in that order -- scale/translate outermost, mirror innermost --
  // because translate()'s percentages always resolve against the element's
  // original (untransformed) box regardless of where a function sits in the
  // chain, so mirroring first and then zooming the (now-mirrored) result
  // works out to exactly the same math already derived above, with
  // scaleX(-1) just prepended as one more step. digitalZoom.rect is already
  // expressed in mirrored space when facing the selfie camera (captureFrame/
  // grabPreviewFrame agree on that), so the rect needs no separate flip here
  // -- only the video's own paint order does.
  const videoTransformParts: string[] = [];
  if (digitalZoom) {
    videoTransformParts.push(
      `scale(${(1 / digitalZoom.rect.w).toFixed(4)})`,
      `translate(${((0.5 - (digitalZoom.rect.x + digitalZoom.rect.w / 2)) * 100).toFixed(2)}%, ${(
        (0.5 - (digitalZoom.rect.y + digitalZoom.rect.h / 2)) *
        100
      ).toFixed(2)}%)`,
    );
  }
  if (mirrored) videoTransformParts.push("scaleX(-1)");

  // Live preview uses the exact same buildFilterCss the review sheet and
  // final capture do, at the stock's default preset -- not a hand-picked
  // CSS approximation covering 2 of 5 stocks. What the guest sees while
  // framing is now what they'll actually get, for every stock, not just
  // the two that happened to get bespoke Tailwind classes.
  //
  // flash's brightness bump is folded into this same string rather than
  // left as a separate Tailwind `brightness-110` class: an inline
  // style.filter (below) overrides a class's `filter` entirely rather than
  // composing with it, so a class-based brightness would silently stop
  // doing anything the moment this was added.
  const liveStockKey = filmStocks[stock]!.key;
  const livePreset = STOCK_PRESETS[liveStockKey] ?? DEFAULT_PRESET;
  const liveFilterCss = buildFilterCss(liveStockKey, livePreset.tone, livePreset.color, livePreset.palette);

  const videoStyle: React.CSSProperties = {
    filter: flash ? `${liveFilterCss} brightness(1.1)` : liveFilterCss,
  };
  if (videoTransformParts.length) videoStyle.transform = videoTransformParts.join(" ");

  return (
    <main className="grain relative min-h-screen overflow-hidden bg-emulsion pb-16">
      <div
        className="relative h-[calc(100svh-3.25rem)] w-full"
        onPointerDown={beginComposing}
        onTouchStart={beginComposing}
      >
        {/* Viewfinder — real camera feed, cropped to the chosen frame ratio */}
        <div className="absolute inset-0 flex items-center justify-center bg-black">
          {cameraError ? (
            <div className="camera-error flex max-w-xs flex-col items-center gap-2 px-6 text-center">
              <p className="font-mono text-[0.65rem] tracking-widest text-safelight">
                CAMERA UNAVAILABLE
              </p>
              <p className="text-sm text-fixer-dim">{cameraError}</p>
              <p className="font-mono text-[0.6rem] tracking-widest text-stale">
                NEEDS HTTPS OR LOCALHOST, AND CAMERA PERMISSION
              </p>
            </div>
          ) : (
            <div
              className={`relative w-full max-h-full overflow-hidden ${RATIOS[ratio]!.cls}`}
              style={{ maxWidth: "100%" }}
            >
              <video
                ref={videoRef}
                autoPlay
                playsInline
                muted
                className="h-full w-full object-cover transition-[filter,transform] duration-500"
                // Inline rather than a Tailwind scale utility: v4 emits those
                // via the `scale` property, not `transform`, which makes the
                // mirror easy to misread as "not applied" when verifying. The
                // analysis frame is mirrored to match in grabPreviewFrame --
                // the two must never disagree or every overlay box lands on
                // the wrong side of its subject.
                style={videoStyle}
              />
              <canvas ref={previewCanvasRef} className="hidden" />
              {/* halation / vignette, stronger the faster the film */}
              <div
                className="pointer-events-none absolute inset-0"
                style={{
                  boxShadow: `inset 0 0 ${40 + iso * 14}px ${12 + iso * 6}px color-mix(in oklab, var(--emulsion) 80%, transparent)`,
                }}
              />

              {/* AI COMPOSE overlay. Everything drawn here is measured, and
                  every element is gated on the engine actually having found
                  something: no subject box when saliency found nothing, no
                  crop rectangle when the crop wouldn't be tighter than the
                  current framing, no film-stock claim when the top two
                  scores are tied. Drawn inside the ratio box (not the
                  screen) because the rects are normalized to the frame that
                  was sent, which is this box's contents. */}
              {aiOn && (
                <div className="pointer-events-none absolute inset-0">
                  {/* Scan particles: only while there's nothing measured yet
                      to show instead -- once a reading lands, ai is set and
                      this scene's "hold still" phase is over, even though
                      aiScanning itself will flip true/false again on every
                      later tick (each ~37ms fetch, per PREVIEW_INTERVAL_MS's
                      docstring). Twinkling forever during routine polling
                      would read as constant uncertainty about a scene the
                      guide has already spoken about. */}
                  {!ai && reachable && aiScanning && (
                    <div className="absolute inset-0 overflow-hidden">
                      {scanDots.map((d, i) => (
                        <span
                          key={i}
                          className="absolute rounded-full bg-drifting"
                          style={{
                            left: `${d.left}%`,
                            top: `${d.top}%`,
                            width: d.size,
                            height: d.size,
                            animation: `scan-twinkle ${d.duration}s ease-in-out ${d.delay}s infinite`,
                          }}
                        />
                      ))}
                    </div>
                  )}

                  {ai?.reframe.subject_found && (
                    <div
                      className="absolute rounded-[3px] border border-drifting/80"
                      style={pct(ai.reframe.subject_box)}
                    >
                      <span className="absolute -top-4 left-0 rounded-sm bg-emulsion/85 px-1 font-mono text-[0.5rem] tracking-[0.16em] text-drifting">
                        SUBJECT
                      </span>
                    </div>
                  )}

                  {/* Auto-zoom already applied this rect to the video's own
                      transform the moment the tick that found it landed (see
                      the preview effect) -- no tap needed, so there's no
                      separate crop-rectangle-plus-button here to keep in
                      sync with it. Just a status chip, and a way to hand
                      control back and forth with the manual ZOOM dial. */}
                  {autoZoom && digitalZoom && (
                    <button
                      type="button"
                      onClick={() => {
                        setAutoZoom(false);
                        setZoomIndex(nearestZoomStep(digitalZoom.zoom));
                      }}
                      className="pointer-events-auto absolute right-2 bottom-2 rounded-sm border border-safelight/60 bg-emulsion/85 px-1.5 py-0.5 font-mono text-[0.5rem] tracking-[0.14em] text-safelight active:scale-95"
                    >
                      AUTO-ZOOMED {digitalZoom.zoom}× · TAP FOR MANUAL
                    </button>
                  )}

                  {!autoZoom && digitalZoom && (
                    <button
                      type="button"
                      onClick={() => {
                        setDigitalZoom(null);
                        setZoomIndex(0);
                        setAutoZoom(true);
                        zoomCandidateRef.current = null;
                        zoomStableCountRef.current = 0;
                      }}
                      className="pointer-events-auto absolute right-2 bottom-2 rounded-sm border border-safelight/60 bg-emulsion/85 px-1.5 py-0.5 font-mono text-[0.5rem] tracking-[0.14em] text-safelight active:scale-95"
                    >
                      ZOOMED {digitalZoom.zoom}× · TAP TO RESET
                    </button>
                  )}

                  {/* Camera move. Anchored to the MIDDLE of the viewfinder, not
                      its bottom: the film strip and shutter row are positioned
                      against the screen, and at phone height (375x812) a
                      bottom-anchored badge lands in exactly the same band as
                      the strip, which paints later and hides it completely.
                      The viewfinder's centre is clear at every frame ratio
                      because the box is centred in the container. */}
                  {ai &&
                    ai.ar_guide.subject_found &&
                    !ai.ar_guide.well_composed &&
                    (() => {
                      const nudge = panInstruction(ai.ar_guide, mirrored);
                      if (!nudge) return null;
                      return (
                        <div className="absolute inset-x-0 top-1/2 flex -translate-y-1/2 flex-col items-center gap-1">
                          <span className="font-display text-5xl leading-none font-bold text-safelight [text-shadow:0_0_12px_color-mix(in_oklab,var(--emulsion)_95%,transparent)]">
                            {nudge.arrows}
                          </span>
                          <span className="rounded-sm bg-emulsion/85 px-2 py-0.5 font-mono text-[0.55rem] tracking-[0.16em] text-safelight">
                            {nudge.text}
                          </span>
                        </div>
                      );
                    })()}

                  {ai?.ar_guide.well_composed && ai.ar_guide.subject_found && (
                    <div className="absolute inset-x-0 top-1/2 flex -translate-y-1/2 justify-center">
                      <span className="rounded-sm bg-emulsion/85 px-2 py-0.5 font-mono text-[0.55rem] tracking-[0.16em] text-converged">
                        ON THE THIRDS · HOLD IT
                      </span>
                    </div>
                  )}

                  {/* Say why there's nothing to show. Saliency genuinely finds
                      no single subject in a diffuse scene (~1 frame in 3 on
                      real photos); a guest deliberately shooting something
                      with no single focal point -- a landscape, a mood shot,
                      a texture -- isn't a failure case, so this says so
                      instead of implying one. */}
                  {ai && !ai.ar_guide.subject_found && (
                    <div className="absolute inset-x-0 top-1/2 flex -translate-y-1/2 justify-center">
                      <span className="rounded-sm bg-emulsion/80 px-2 py-0.5 font-mono text-[0.55rem] tracking-[0.16em] text-stale">
                        OPEN SCENE · SHOOT FREELY
                      </span>
                    </div>
                  )}

                  {/* The reason banner and the score readout are NOT here --
                      see the container-level block below. Only things that
                      must line up with frame coordinates (the subject box,
                      the crop rect, the nudge) belong inside the viewfinder;
                      text pinned to its edges lands underneath the camera's
                      own chrome. */}
                </div>
              )}
              {stamp && (
                <p className="pointer-events-none absolute right-3 bottom-3 font-mono text-[0.7rem] tracking-widest text-safelight/90 [text-shadow:0_0_6px_color-mix(in_oklab,var(--safelight)_60%,transparent)]">
                  {today} {reachable ? "" : "•"}
                </p>
              )}
            </div>
          )}
        </div>

        {/* top strip: exposure index + queue state, read like a camera's LCD */}
        <div className="absolute inset-x-0 top-0 flex items-start justify-between gap-2 p-4">
          <div className="rounded-sm bg-emulsion/70 px-2.5 py-1.5 backdrop-blur">
            <p className="font-mono text-[0.65rem] tracking-widest text-fixer">
              EI {ISO[iso]} · {SHUTTER[shutter]} · f/2.0
            </p>
            <p className="font-mono text-[0.55rem] tracking-widest text-fixer-dim">
              FRAME {String(frame).padStart(2, "0")} · {RATIOS[ratio]!.key}
            </p>
          </div>
          <div className="rounded-sm bg-emulsion/70 px-2.5 py-1.5 text-right backdrop-blur">
            {reachable ? (
              <p className="font-mono text-[0.65rem] tracking-widest text-converged">
                ROLL HANDED AROUND
              </p>
            ) : (
              <p className="font-mono text-[0.65rem] tracking-widest text-drifting">
                {queued} FRAMES WAITING ON THE ROLL
              </p>
            )}
          </div>
        </div>

        {/* zone strip: which corner of the room this frame belongs to */}
        <div className="absolute inset-x-0 top-16 px-4">
          <p className="font-mono text-[0.5rem] tracking-[0.18em] text-fixer-dim">STANDING AT</p>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {ZONES.map((z) => (
              <button
                key={z}
                onClick={() => setZone(z)}
                className={`rounded-sm border px-2 py-1 font-mono text-[0.58rem] tracking-widest capitalize transition ${
                  z === zone
                    ? "border-drifting bg-drifting/20 text-drifting"
                    : "border-fixer/20 bg-emulsion/60 text-fixer-dim"
                }`}
              >
                {prettyZone(z)}
              </button>
            ))}
          </div>
        </div>

        {/* composition guidance — photography language only */}
        <div className="pointer-events-none absolute inset-0">
          {grid && (
            <>
              <div className="absolute inset-x-8 top-1/3 h-px bg-fixer/20" />
              <div className="absolute inset-x-8 top-2/3 h-px bg-fixer/20" />
              <div className="absolute inset-y-16 left-1/3 w-px bg-fixer/20" />
              <div className="absolute inset-y-16 left-2/3 w-px bg-fixer/20" />
            </>
          )}

          {/* AI compose text, anchored to the SCREEN rather than to the
              viewfinder. The camera's own chrome -- the EI/FRAME readout at
              top-0 and the zone chips at top-16 -- is positioned against the
              container, so anything pinned to the viewfinder's top edge
              renders underneath it: on a phone this clipped the reason
              sentence mid-word ("...rops the color and leans on contrast").
              At 375px wide the chips already wrap to two rows and end at
              y=133; 10rem leaves room for a third row on a narrower phone
              while still sitting well above the nudge at the viewfinder's
              centre (y~345). */}
          {aiOn && (
            <div className="absolute inset-x-0 top-[10rem] flex flex-col items-center gap-1 px-4">
              {ai ? (
                <span className="max-w-full rounded-sm bg-emulsion/85 px-2 py-1 text-center font-mono text-[0.55rem] leading-relaxed tracking-[0.1em] text-fixer">
                  {ai.reason}
                </span>
              ) : !reachable ? (
                <span className="rounded-sm bg-emulsion/85 px-2 py-1 font-mono text-[0.55rem] tracking-[0.16em] text-stale">
                  GUIDANCE OFFLINE · SHUTTER UNAFFECTED
                </span>
              ) : aiScanning ? (
                <span className="settling rounded-sm bg-emulsion/85 px-2 py-1 font-mono text-[0.55rem] tracking-[0.16em] text-drifting">
                  READING THE LIGHT · HOLD STILL
                </span>
              ) : null}
              {ai && (
                <span className="rounded-sm bg-emulsion/80 px-1.5 py-0.5 font-mono text-[0.5rem] tracking-[0.14em] text-fixer-dim">
                  LOOKS {ai.aesthetic_score.toFixed(1)}/10
                  {!ai.confident && " · NO STRONG STOCK"}
                </span>
              )}
            </div>
          )}

          {showNudge && topZone && topZone.zone !== zone && (
            <div className="absolute top-[20%] right-[8%] w-[46%] settling">
              <div className="h-40 w-full rounded-sm border border-drifting/70" />
              <p className="mt-2 text-right font-display text-sm font-semibold text-drifting/90">
                {prettyZone(topZone.zone)} is drawing the room
              </p>
            </div>
          )}

          {count !== null && (
            <div className="absolute inset-0 flex items-center justify-center">
              <span className="font-display text-[6rem] leading-none font-extrabold text-fixer/90">
                {count}
              </span>
            </div>
          )}
        </div>

        {/* scrim so the controls stay legible over a bright room */}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-72 bg-gradient-to-t from-emulsion via-emulsion/85 to-transparent" />

        {/* SETTINGS PANEL — the "advanced" drawer of a retro cam */}
        {panel && (
          <div className="absolute inset-x-0 bottom-[16.5rem] px-4">
            <div className="film-edge rounded-sm border border-fixer/20 bg-emulsion/90 px-3 py-5 backdrop-blur">
              <div className="flex gap-2">
                <Dial label="ISO" values={ISO} index={iso} onChange={setIso} />
                <Dial label="SHUTTER" values={SHUTTER} index={shutter} onChange={setShutter} />
                <Dial label="EV" values={EV} index={ev} onChange={setEv} />
              </div>

              <div className="mt-2 flex gap-2">
                <Dial
                  label="ZOOM"
                  values={ZOOM_STEPS.map((z) => `${z}×`)}
                  index={zoomIndex}
                  onChange={onManualZoomChange}
                />
              </div>

              <div className="mt-3 flex flex-wrap gap-1.5">
                <Toggle on={flash} onClick={() => setFlash((v) => !v)}>
                  FLASH {flash ? "ON" : "OFF"}
                </Toggle>
                <Toggle on={grid} onClick={() => setGrid((v) => !v)}>
                  GRID
                </Toggle>
                <Toggle on={aiOn} onClick={() => setAiOn((v) => !v)}>
                  AI COMPOSE {aiOn ? "ON" : "OFF"}
                </Toggle>
                <Toggle
                  on={autoZoom}
                  onClick={() =>
                    setAutoZoom((v) => {
                      if (v) {
                        // Turning auto off: freeze the ZOOM dial at wherever
                        // auto-zoom currently has it, so switching to manual
                        // doesn't jump the frame.
                        setZoomIndex(digitalZoom ? nearestZoomStep(digitalZoom.zoom) : 0);
                      } else {
                        // Turning auto back on: start the stability count
                        // fresh rather than resuming whatever it was mid-way
                        // through when auto got switched off.
                        zoomCandidateRef.current = null;
                        zoomStableCountRef.current = 0;
                      }
                      return !v;
                    })
                  }
                >
                  AUTO ZOOM {autoZoom ? "ON" : "OFF"}
                </Toggle>
                <Toggle on={stamp} onClick={() => setStamp((v) => !v)}>
                  DATE STAMP
                </Toggle>
                <Toggle on={burst} onClick={() => setBurst((v) => !v)}>
                  BURST ×3
                </Toggle>
                <Toggle
                  on={timer > 0}
                  onClick={() => setTimer((t) => (t === 0 ? 3 : t === 3 ? 10 : 0))}
                >
                  TIMER {timer === 0 ? "OFF" : `${timer}s`}
                </Toggle>
                {RATIOS.map((r, i) => (
                  <Toggle key={r.key} on={i === ratio} onClick={() => setRatio(i)}>
                    {r.key.toUpperCase()}
                  </Toggle>
                ))}
              </div>

              <p className="mt-3 font-mono text-[0.55rem] leading-relaxed tracking-widest text-stale">
                SETTINGS ARE LOCAL TO THIS PHONE. THEY TRAVEL WITH THE FRAME, NOT WITH THE ROOM.
              </p>
            </div>
          </div>
        )}

        {/* film strip: physical filters flicked over the feed */}
        <div className="absolute inset-x-0 bottom-40">
          <div className="film-edge overflow-x-auto bg-emulsion/60 py-3 backdrop-blur [scrollbar-width:none]">
            <div className="flex gap-2 px-4">
              {filmStocks.map((f, i) => {
                // Marked in place rather than sorted to the front: the strip
                // re-reads every 1.5s, and cards sliding around under a
                // thumb that's already reaching for one is a worse trade
                // than a badge.
                const recommended = aiOn && ai?.confident && ai.suggested_filter === f.key;
                return (
                  <button
                    key={f.key}
                    onClick={() => {
                      stockTouched.current = true; // stop auto-applying from here on
                      setStock(i);
                    }}
                    className={`relative min-w-[7.5rem] shrink-0 rounded-sm border px-3 py-2 text-left transition ${
                      i === stock
                        ? "border-drifting bg-drifting/15"
                        : recommended
                          ? "border-safelight/60 bg-emulsion/40"
                          : "border-fixer/20 bg-emulsion/40"
                    }`}
                  >
                    {recommended && (
                      <span className="absolute -top-1.5 right-1 rounded-sm bg-safelight px-1 font-mono text-[0.45rem] tracking-[0.14em] text-emulsion">
                        AI PICK
                      </span>
                    )}
                    <span className="block font-display text-[0.82rem] leading-tight font-bold text-fixer">
                      {f.name}
                    </span>
                    <span className="block font-mono text-[0.6rem] tracking-wide text-fixer-dim">
                      {f.note}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* shutter row */}
        <div className="absolute inset-x-0 bottom-6 flex items-center justify-between gap-4 px-6">
          <button
            onClick={() => setPanel((p) => !p)}
            className={`w-20 rounded-sm border px-2 py-2 text-left font-mono text-[0.55rem] leading-tight tracking-widest ${
              panel ? "border-drifting text-drifting" : "border-fixer/25 text-fixer-dim"
            }`}
          >
            {panel ? "CLOSE" : "SETTINGS"}
            <br />
            EV {EV[ev]}
          </button>
          <button
            onClick={onShutter}
            disabled={!!cameraError}
            aria-label="Take a photo"
            className="h-20 w-20 rounded-full border-4 border-fixer/80 bg-fixer/90 active:scale-95 transition disabled:opacity-40"
          />
          <div className="flex w-20 flex-col items-end gap-1.5">
            <button
              onClick={() => setFacing((f) => (f === "environment" ? "user" : "environment"))}
              aria-label={mirrored ? "Switch to rear camera" : "Switch to front camera"}
              className="flex items-center gap-1 rounded-sm border border-fixer/25 px-1.5 py-1 font-mono text-[0.5rem] tracking-widest text-fixer-dim active:bg-fixer/10"
            >
              <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M3 12a9 9 0 0 1 15-6.7L21 8" />
                <polyline points="21 3 21 8 16 8" />
                <path d="M21 12a9 9 0 0 1-15 6.7L3 16" />
                <polyline points="3 21 3 16 8 16" />
              </svg>
              {mirrored ? "FRONT" : "REAR"}
            </button>
            <p className="text-right font-mono text-[0.55rem] leading-tight tracking-widest text-fixer-dim">
              {(filmStocks[stock]?.name ?? "").split(" ")[0]?.toUpperCase()}
              <br />
              {flash ? "FLASH ⚡" : "NO FLASH"}
            </p>
          </div>
        </div>

        {/* Post-capture review — a single (non-burst) frame lands here before
            it ever reaches the outbox. Opaque and last in DOM order, so it
            covers the viewfinder and shutter row beneath it without needing
            an explicit z-index. */}
        {reviewing && (
          <div className="absolute inset-0 flex flex-col bg-emulsion">
            <div className="relative flex-1 overflow-hidden bg-black">
              <img
                src={reviewing.previewUrl}
                alt="Captured frame"
                className="h-full w-full object-contain"
                style={{
                  filter: buildFilterCss(
                    filmStocks[reviewing.stock]!.key,
                    reviewing.tone,
                    reviewing.color,
                    reviewing.palette,
                  ),
                }}
              />
              <span className="absolute top-2 left-2 rounded-sm bg-emulsion/85 px-2 py-1 font-mono text-[0.55rem] tracking-[0.16em] text-drifting">
                REVIEW · {filmStocks[reviewing.stock]!.name}
              </span>
            </div>

            <div className="film-edge border-t border-fixer/20 bg-emulsion/95 px-4 py-4 backdrop-blur">
              <div className="flex gap-2 overflow-x-auto pb-1 [scrollbar-width:none]">
                {filmStocks.map((f, i) => (
                  <button
                    key={f.key}
                    onClick={() => {
                      const preset = STOCK_PRESETS[f.key] ?? DEFAULT_PRESET;
                      setReviewing((r) => (r ? { ...r, stock: i, ...preset } : r));
                    }}
                    className={`shrink-0 rounded-sm border px-2.5 py-1.5 font-mono text-[0.55rem] tracking-widest ${
                      i === reviewing.stock
                        ? "border-drifting bg-drifting/20 text-drifting"
                        : "border-fixer/20 text-fixer-dim"
                    }`}
                  >
                    {f.name}
                  </button>
                ))}
              </div>

              <p className="mt-2 font-mono text-[0.55rem] leading-relaxed tracking-[0.08em] text-fixer-dim">
                {ai?.reason ?? filmStocks[reviewing.stock]!.note}
              </p>

              <div className="mt-3 grid grid-cols-3 gap-3">
                <Slider
                  label="TONE"
                  value={reviewing.tone}
                  min={-100}
                  max={100}
                  onChange={(v) => setReviewing((r) => (r ? { ...r, tone: v } : r))}
                />
                <Slider
                  label="COLOR"
                  value={reviewing.color}
                  min={0}
                  max={150}
                  onChange={(v) => setReviewing((r) => (r ? { ...r, color: v } : r))}
                />
                <Slider
                  label="PALETTE"
                  value={reviewing.palette}
                  min={0}
                  max={100}
                  onChange={(v) => setReviewing((r) => (r ? { ...r, palette: v } : r))}
                />
              </div>

              <div className="mt-4 flex gap-3">
                <button
                  onClick={retakeReview}
                  className="flex-1 rounded-sm border border-fixer/30 py-2.5 font-mono text-[0.6rem] tracking-widest text-fixer-dim active:scale-95"
                >
                  RETAKE
                </button>
                <button
                  onClick={confirmReview}
                  className="flex-1 rounded-sm border border-drifting bg-drifting/20 py-2.5 font-mono text-[0.6rem] font-bold tracking-widest text-drifting active:scale-95"
                >
                  USE THIS FRAME
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <GuestTabs active="capture" />
    </main>
  );
}

/** Center-crops the live video to `ar`, then further crops to `zoomRect`
 * (if applied) -- the same absolute rect the CSS transform is showing on
 * screen -- and draws the result onto a fresh canvas at full sensor
 * resolution. Used for both the burst path (immediate queue) and the
 * single-shot review path (frozen onto its own canvas, edited, then
 * queued), so "zoom in" and the review sheet both operate on exactly what
 * the viewfinder displayed, not the untouched raw frame.
 *
 * The saved photo itself is never pixel-mirrored, front camera or not --
 * matching how the shutter has always behaved here, selfie or rear. `zoomRect`
 * is expressed in mirrored (as-displayed) space when facing the selfie
 * camera though, so its x still needs flipping before it can select raw
 * sensor pixels -- same reasoning as grabPreviewFrame, minus that function's
 * final mirror-the-output step, since this one deliberately skips it. */
function captureFrame(
  video: HTMLVideoElement | null,
  zoomRect: { x: number; y: number; w: number; h: number } | null | undefined,
  ar: number,
  mirrored: boolean,
): HTMLCanvasElement | null {
  if (!video || video.videoWidth === 0) return null;
  const vw = video.videoWidth;
  const vh = video.videoHeight;

  let sx = 0, sy = 0, sw = vw, sh = vh;
  if (zoomRect) {
    const cropW = Math.min(vw, vh * ar);
    const cropH = cropW / ar;
    const baseX = (vw - cropW) / 2;
    const baseY = (vh - cropH) / 2;
    const rectX = mirrored ? 1 - zoomRect.x - zoomRect.w : zoomRect.x;
    sx = baseX + rectX * cropW;
    sy = baseY + zoomRect.y * cropH;
    sw = zoomRect.w * cropW;
    sh = zoomRect.h * cropH;
  }

  const canvas = document.createElement("canvas");
  canvas.width = sw;
  canvas.height = sh;
  canvas.getContext("2d")!.drawImage(video, sx, sy, sw, sh, 0, 0, sw, sh);
  return canvas;
}

/** Center-crops the live video to `ar` -- exactly how the viewfinder shows
 * it with object-cover -- then downscales to PREVIEW_EDGE_PX and returns
 * bare base64 JPEG.
 *
 * The crop is a correctness requirement, not just bandwidth thrift.
 * /analyze/preview returns rectangles normalized to whatever frame it was
 * handed, and the overlay paints them straight onto the displayed video
 * box. Send the raw uncropped sensor frame and every box lands visibly
 * offset from the thing it claims to be pointing at. */
function grabPreviewFrame(
  video: HTMLVideoElement,
  canvas: HTMLCanvasElement,
  ar: number,
  mirrored: boolean,
  zoomRect?: { x: number; y: number; w: number; h: number },
): string | null {
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh) return null;

  const cropW = Math.min(vw, vh * ar);
  const cropH = cropW / ar;
  let sx = (vw - cropW) / 2;
  let sy = (vh - cropH) / 2;
  let sw = cropW;
  let sh = cropH;
  // Applied zoom is a sub-rect of the ar-crop above, in the same normalized
  // space the on-screen CSS transform uses -- crop to it here too, so the AI
  // reads the zoomed-in view the guest is actually looking at, not the full
  // scene it already recommended zooming out of.
  if (zoomRect) {
    // zoomRect.x is expressed in mirrored (as-displayed) space when facing
    // the selfie camera -- flip it before selecting raw sensor pixels.
    // Cropping window [a, a+w] of a mirrored frame is the same pixels as
    // cropping [1-a-w, 1-a] of the raw frame and then mirroring just that
    // crop, which is exactly what the mirror step below does.
    const rectX = mirrored ? 1 - zoomRect.x - zoomRect.w : zoomRect.x;
    sx += rectX * cropW;
    sy += zoomRect.y * cropH;
    sw = zoomRect.w * cropW;
    sh = zoomRect.h * cropH;
  }

  const scale = PREVIEW_EDGE_PX / Math.max(sw, sh);
  canvas.width = Math.max(1, Math.round(sw * scale));
  canvas.height = Math.max(1, Math.round(sh * scale));
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  // Selfie view is displayed mirrored, so mirror what we analyze too.
  // Otherwise every returned rect is drawn on the opposite side of the
  // subject it describes -- the boxes must live in the same coordinate
  // space as the pixels the guest is actually looking at.
  if (mirrored) {
    ctx.setTransform(-1, 0, 0, 1, canvas.width, 0);
  } else {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }
  ctx.drawImage(video, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  return canvas.toDataURL("image/jpeg", 0.8).split(",")[1] ?? null;
}

/** Normalized 0..1 rect -> CSS percentages, so the overlay scales with the
 * viewfinder at any screen size without knowing its pixel dimensions. */
function pct(r: { x: number; y: number; w: number; h: number }) {
  return {
    left: `${r.x * 100}%`,
    top: `${r.y * 100}%`,
    width: `${r.w * 100}%`,
    height: `${r.h * 100}%`,
  };
}

/** A plain centered crop at `zoom`, for the manual ZOOM dial -- unlike
 * auto-zoom's absolute-against-the-full-frame rect (see the preview tick),
 * this doesn't try to track any subject, just centers on whatever's already
 * in the middle. Manually dialing zoom is a deliberate "just make it
 * tighter/looser" action, not a refinement of the AI's guess. */
function centeredZoomRect(zoom: number) {
  const size = 1 / zoom;
  const off = (1 - size) / 2;
  return { x: off, y: off, w: size, h: size };
}

/** Index of the ZOOM_STEPS entry closest to `zoom`, so the manual dial can
 * reflect a zoom level the AI's tap-to-zoom just set (an arbitrary, possibly
 * off-center value) without the two controls visibly disagreeing. */
function nearestZoomStep(zoom: number): number {
  let best = 0;
  let bestDiff = Infinity;
  ZOOM_STEPS.forEach((z, i) => {
    const diff = Math.abs(z - zoom);
    if (diff < bestDiff) {
      best = i;
      bestDiff = diff;
    }
  });
  return best;
}

/** "Close enough to call the same framing" for auto-zoom's stability check
 * (see zoomCandidateRef) -- position within 6% of the frame and size within
 * 6% counts as the guest holding roughly still, not a new recommendation. */
function rectsSimilar(
  a: { x: number; y: number; w: number; h: number },
  b: { x: number; y: number; w: number; h: number },
  tol = 0.06,
): boolean {
  return (
    Math.abs(a.x - b.x) < tol &&
    Math.abs(a.y - b.y) < tol &&
    Math.abs(a.w - b.w) < tol &&
    Math.abs(a.h - b.h) < tol
  );
}

/** TONE/COLOR/PALETTE -> a real canvas `filter` string, applied both live
 * (the review sheet's <img> style, for instant feedback) and baked into
 * the saved photo (confirmReview draws through this same string) -- one
 * formula, not a preview effect that quietly doesn't survive the save.
 *
 * TONE drives brightness/contrast around neutral; COLOR is a direct
 * saturation percentage (0 = grayscale, 100 = unchanged); PALETTE (0..100)
 * scales each stock's own characteristic secondary treatment, so turning
 * it down fades toward "no particular stock" rather than toward black. */
function buildFilterCss(stockKey: string, tone: number, color: number, palette: number): string {
  const brightness = 1 + tone / 250;
  const contrast = 1 + Math.abs(tone) / 400;
  const saturate = color / 100;
  const p = palette / 100;

  const parts = [
    `brightness(${brightness.toFixed(3)})`,
    `contrast(${contrast.toFixed(3)})`,
    `saturate(${saturate.toFixed(3)})`,
  ];

  switch (stockKey) {
    case "tri_x_400":
      parts.push(`grayscale(${p.toFixed(3)})`, `contrast(${(1 + 0.25 * p).toFixed(3)})`);
      break;
    case "cinestill_800t":
      parts.push(`hue-rotate(${(-6 * p).toFixed(1)}deg)`, `saturate(${(1 + 0.3 * p).toFixed(3)})`);
      break;
    case "ektar_100":
      parts.push(`saturate(${(1 + 0.4 * p).toFixed(3)})`);
      break;
    case "gold_200":
      parts.push(`sepia(${(0.25 * p).toFixed(3)})`);
      break;
    case "portra_400":
      parts.push(`sepia(${(0.12 * p).toFixed(3)})`, `saturate(${(1 - 0.1 * p).toFixed(3)})`);
      break;
  }
  return parts.join(" ");
}

const STRENGTH_COPY: Record<string, string> = {
  slight: "slightly",
  moderate: "a little",
  large: "a good way",
};

/** Instructions are phrased as camera moves, so they read from
 * pan_camera_*, NOT move_subject_* -- those two are inverses of each
 * other (to push the subject right in frame you turn the camera left).
 * Picking the wrong field renders correct arithmetic as a backwards
 * arrow, which is worse than showing nothing at all because it still
 * looks authoritative. See ai_engine.compute_ar_guide's docstring. */
function panInstruction(
  g: ComposePreview["ar_guide"],
  mirrored: boolean,
): { arrows: string; text: string } | null {
  // Horizontal mirroring flips the relationship between screen direction and
  // physical camera motion, so the selfie camera needs the OTHER field:
  //
  //   rear (not mirrored): to push the subject right in frame, pan left.
  //     -> pan_camera_x, which is already the inverse of move_subject_x.
  //   front (mirrored):    the frame we analyzed is itself mirrored, so
  //     "subject moves right on screen" means "moves left in the raw sensor
  //     frame", which needs a pan RIGHT -- i.e. the same word as
  //     move_subject_x, not its inverse.
  //
  // Vertical is untouched by a horizontal mirror, so tilt always reads
  // pan_camera_y. Getting this wrong gives a confidently backwards arrow on
  // exactly one of the two cameras, which is the hardest kind to notice.
  const xDir = mirrored ? g.move_subject_x : g.pan_camera_x;
  const arrows: string[] = [];
  const words: string[] = [];
  if (xDir !== "centered") {
    arrows.push(xDir === "left" ? "←" : "→");
    words.push(xDir === "left" ? "PAN LEFT" : "PAN RIGHT");
  }
  if (g.pan_camera_y !== "centered") {
    arrows.push(g.pan_camera_y === "up" ? "↑" : "↓");
    words.push(g.pan_camera_y === "up" ? "TILT UP" : "TILT DOWN");
  }
  if (!words.length) return null;
  const how = STRENGTH_COPY[g.strength];
  return { arrows: arrows.join(" "), text: `${words.join(" · ")}${how ? ` ${how}` : ""}` };
}

/** This backend has no photo-binary storage of its own -- the only way
 * another guest's device can ever see this capture is if the bytes ride
 * along inside the photo event itself. Chunked to avoid a call-stack
 * blowup from spreading a large Uint8Array into String.fromCharCode at
 * once. Same helper as the Phase 5 reference client's sync.ts. */
async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const chunkSize = 8192;
  let binary = "";
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}
