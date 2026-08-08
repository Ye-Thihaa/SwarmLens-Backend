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

function Capture() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // Separate from canvasRef on purpose: the preview loop and a shutter press
  // can land in the same tick, and shootOnce resizes its canvas to the full
  // sensor frame. Sharing one canvas would let a capture and an analysis
  // scribble over each other's pixels.
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

  const [stock, setStock] = useState(0);
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
  useEffect(() => {
    if (!aiOn || cameraError) {
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
        const frame = grabPreviewFrame(video!, canvas!, ar, mirrored);
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
  }, [aiOn, cameraError, ratio, mirrored]);

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

  /** Grabs the current video frame into a real JPEG, queues it in the
   * offline outbox immediately (the shutter never waits on the network),
   * then fires a background sync attempt. Burst just repeats this. */
  function shootOnce() {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas || video.videoWidth === 0) return;
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")!.drawImage(video, 0, 0);
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

  function shoot() {
    const n = burst ? 3 : 1;
    for (let i = 0; i < n; i++) shootOnce();
  }

  function onShutter() {
    beginComposing();
    if (timer > 0) setCount(timer);
    else shoot();
  }

  const today = "26 07 08";

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
                className={`h-full w-full object-cover transition-[filter] duration-500 ${
                  stock === 2
                    ? "grayscale contrast-125"
                    : stock === 1
                      ? "saturate-150 hue-rotate-[-8deg]"
                      : ""
                } ${flash ? "brightness-110" : ""}`}
                // Inline rather than a Tailwind scale utility: v4 emits those
                // via the `scale` property, not `transform`, which makes the
                // mirror easy to misread as "not applied" when verifying. The
                // analysis frame is mirrored to match in grabPreviewFrame --
                // the two must never disagree or every overlay box lands on
                // the wrong side of its subject.
                style={mirrored ? { transform: "scaleX(-1)" } : undefined}
              />
              <canvas ref={canvasRef} className="hidden" />
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

                  {ai?.reframe.worth_it && (
                    <div
                      className="settling absolute rounded-lg border-2 border-safelight/85"
                      style={pct(ai.reframe.rect)}
                    >
                      <span className="absolute -bottom-5 right-0 rounded-sm bg-emulsion/85 px-1 font-mono text-[0.55rem] tracking-[0.16em] text-safelight">
                        {ai.reframe.zoom}× TIGHTER
                      </span>
                    </div>
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
                      real photos), and silent absence is indistinguishable
                      from the feature being broken -- which is exactly how
                      this looked on a phone before. */}
                  {ai && !ai.ar_guide.subject_found && (
                    <div className="absolute inset-x-0 top-1/2 flex -translate-y-1/2 justify-center">
                      <span className="rounded-sm bg-emulsion/80 px-2 py-0.5 font-mono text-[0.55rem] tracking-[0.16em] text-stale">
                        NO CLEAR SUBJECT
                      </span>
                    </div>
                  )}

                  {/* Top banner: scanning until the first real reading lands,
                      then the measured reason for the recommendation. */}
                  <div className="absolute inset-x-0 top-0 flex justify-center p-2">
                    {ai ? (
                      <span className="max-w-[92%] rounded-sm bg-emulsion/85 px-2 py-1 text-center font-mono text-[0.55rem] leading-relaxed tracking-[0.1em] text-fixer">
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
                  </div>

                  {ai && (
                    <span className="absolute bottom-2 left-2 rounded-sm bg-emulsion/80 px-1.5 py-0.5 font-mono text-[0.5rem] tracking-[0.14em] text-fixer-dim">
                      LOOKS {ai.aesthetic_score.toFixed(1)}/10
                      {!ai.confident && " · NO STRONG STOCK"}
                    </span>
                  )}
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
      </div>

      <GuestTabs active="capture" />
    </main>
  );
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
): string | null {
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh) return null;

  const cropW = Math.min(vw, vh * ar);
  const cropH = cropW / ar;
  const sx = (vw - cropW) / 2;
  const sy = (vh - cropH) / 2;

  const scale = PREVIEW_EDGE_PX / Math.max(cropW, cropH);
  canvas.width = Math.max(1, Math.round(cropW * scale));
  canvas.height = Math.max(1, Math.round(cropH * scale));
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
  ctx.drawImage(video, sx, sy, cropW, cropH, 0, 0, canvas.width, canvas.height);
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
