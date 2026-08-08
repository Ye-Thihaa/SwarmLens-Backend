import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { filmStocks } from "@/guest/data";
import { GuestTabs } from "@/guest/ui";
import { pickNode, prettyZone, ZONES, type ZoneScore, getZones } from "@/lib/api";
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
const RATIOS = [
  { key: "3:4", cls: "aspect-[3/4]" },
  { key: "1:1", cls: "aspect-square" },
  { key: "strip", cls: "aspect-[1/3]" },
] as const;

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
  const [cameraError, setCameraError] = useState<string | null>(null);

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

  // real camera feed, not a bundled photo
  useEffect(() => {
    let stream: MediaStream | null = null;
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "environment" }, audio: false })
      .then((s) => {
        stream = s;
        if (videoRef.current) videoRef.current.srcObject = s;
      })
      .catch((e) => setCameraError(String(e)));
    return () => stream?.getTracks().forEach((t) => t.stop());
  }, []);

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
              />
              <canvas ref={canvasRef} className="hidden" />
              {/* halation / vignette, stronger the faster the film */}
              <div
                className="pointer-events-none absolute inset-0"
                style={{
                  boxShadow: `inset 0 0 ${40 + iso * 14}px ${12 + iso * 6}px color-mix(in oklab, var(--emulsion) 80%, transparent)`,
                }}
              />
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
              {filmStocks.map((f, i) => (
                <button
                  key={f.name}
                  onClick={() => setStock(i)}
                  className={`min-w-[7.5rem] shrink-0 rounded-sm border px-3 py-2 text-left transition ${
                    i === stock
                      ? "border-drifting bg-drifting/15"
                      : "border-fixer/20 bg-emulsion/40"
                  }`}
                >
                  <span className="block font-display text-[0.82rem] leading-tight font-bold text-fixer">
                    {f.name}
                  </span>
                  <span className="block font-mono text-[0.6rem] tracking-wide text-fixer-dim">
                    {f.note}
                  </span>
                </button>
              ))}
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
          <p className="w-20 text-right font-mono text-[0.55rem] leading-tight tracking-widest text-fixer-dim">
            {(filmStocks[stock]?.name ?? "").split(" ")[0]?.toUpperCase()}
            <br />
            LOADED
            <br />
            {flash ? "FLASH ⚡" : "NO FLASH"}
          </p>
        </div>
      </div>

      <GuestTabs active="capture" />
    </main>
  );
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
