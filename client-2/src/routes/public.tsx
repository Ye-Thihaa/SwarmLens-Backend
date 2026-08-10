import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { GuestTabs } from "@/guest/ui";
import { getPublicPhotos, photoImageUrl, pickNode, prettyZone, type RemotePhoto } from "@/lib/api";
import { useCurrentEvent } from "@/lib/event";

export const Route = createFileRoute("/public")({
  head: () => ({
    meta: [
      { title: "Public gallery — SwarmLens" },
      {
        name: "description",
        content:
          "The best of tonight, hand-picked by the guests who shot it — open to anyone, no app required to look.",
      },
      { property: "og:title", content: "Public gallery — SwarmLens" },
      {
        property: "og:description",
        content: "Guests' own picks from the room, laid out for the world outside it.",
      },
    ],
  }),
  component: PublicGallery,
});

/** Deterministic string hash (FNV-1a) -- same photo_id always produces the
 * same tile size, so the wall doesn't reshuffle itself on every 4s poll
 * just because a photo further down the list arrived. A real RNG seeded
 * once per mount would do that; this doesn't need seeding or state at all. */
function hashStr(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

type Tile = { colSpan: number; rowSpan: number };

/** Mirrors the reference gallery-wall sheets: mostly small squares and
 * dominoes with a scattering of bigger anchor tiles, never uniform, never
 * one giant tile hogging the whole wall. Weighted so "small" wins most
 * rolls -- a wall of all-hero tiles reads as a grid, not a collage. */
function tileFor(photoId: string): Tile {
  const h = hashStr(photoId);
  const r = h % 100;
  const alt = (h >>> 8) % 2 === 0;
  if (r < 45) return { colSpan: 1, rowSpan: 1 };
  if (r < 68) return alt ? { colSpan: 1, rowSpan: 2 } : { colSpan: 2, rowSpan: 1 };
  if (r < 88) return { colSpan: 2, rowSpan: 2 };
  if (r < 97) return alt ? { colSpan: 2, rowSpan: 3 } : { colSpan: 3, rowSpan: 2 };
  return { colSpan: 3, rowSpan: 3 };
}

function timeOf(p: RemotePhoto): string {
  return new Date(p.taken_at * 1000).toLocaleTimeString("en-GB", { hour12: false });
}

async function downloadPhoto(node: string, photo: RemotePhoto) {
  const res = await fetch(photoImageUrl(node, photo.photo_id));
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `swarmlens-${photo.photo_id}.jpg`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function PublicGallery() {
  const event = useCurrentEvent();
  const [node, setNode] = useState<string | null>(null);
  const [photos, setPhotos] = useState<RemotePhoto[]>([]);
  const [open, setOpen] = useState<RemotePhoto | null>(null);
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const n = await pickNode();
      if (cancelled) return;
      setNode(n);
      if (!n) return;
      try {
        const ps = await getPublicPhotos(n, event.event_id);
        if (!cancelled) setPhotos(ps);
      } catch {
        // stay on the last known-good read
      }
    }
    void poll();
    const id = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [event.event_id]);

  const contributors = useMemo(() => new Set(photos.map((p) => p.guest_id)).size, [photos]);

  // Order is stable across polls (sorted by photo_id, not arrival time),
  // so the wall doesn't jump every new photo further up the list -- new
  // arrivals settle in wherever their hash puts them, same as any other
  // tile, and the settling pulse (shared with every other gallery tab)
  // is what actually announces "this one's new".
  const ordered = useMemo(() => [...photos].sort((a, b) => a.photo_id.localeCompare(b.photo_id)), [photos]);

  return (
    <main className="grain min-h-screen pb-24">
      <header className="border-b border-border px-5 pt-8 pb-5">
        <p className="label-mono">PUBLIC GALLERY</p>
        <h1 className="mt-2 text-3xl font-extrabold">{event.name}</h1>
        <p className="mt-2 max-w-sm text-sm leading-relaxed text-muted-foreground">
          Guests picked these themselves — up to 25 shots each — for anyone to see and take home, not
          just the room. Tap a frame to open it and download the full image.
        </p>
        <p className="mt-3 font-mono text-[0.6rem] tracking-widest text-fixer-dim">
          {photos.length} PHOTO{photos.length === 1 ? "" : "S"} · {contributors} GUEST
          {contributors === 1 ? "" : "S"} ·{" "}
          {node ? <span className="text-converged">LIVE</span> : <span className="text-safelight">NO NODE REACHABLE</span>}
        </p>
      </header>

      {photos.length === 0 ? (
        <section className="px-5 py-10 text-center">
          <p className="text-sm text-muted-foreground">
            {node
              ? "Nobody's published a public pick yet — check back once someone hits MAKE PUBLIC on My roll."
              : "Waiting for a node to answer…"}
          </p>
        </section>
      ) : (
        <section
          className="grid gap-1.5 px-1.5 py-5"
          style={{
            gridTemplateColumns: "repeat(auto-fill, minmax(72px, 1fr))",
            gridAutoRows: "72px",
            gridAutoFlow: "dense",
          }}
        >
          {ordered.map((p) => {
            const tile = tileFor(p.photo_id);
            return (
              <button
                key={p.photo_id}
                onClick={() => setOpen(p)}
                style={{ gridColumn: `span ${tile.colSpan}`, gridRow: `span ${tile.rowSpan}` }}
                className="group relative overflow-hidden rounded-sm border border-border bg-card"
              >
                <img
                  src={photoImageUrl(node ?? "", p.photo_id)}
                  alt={`${prettyZone(p.zone)} by ${p.guest_id}`}
                  loading="lazy"
                  className="h-full w-full object-cover"
                />
                <span className="absolute inset-x-0 bottom-0 bg-emulsion/85 px-1.5 py-1 text-left font-mono text-[0.45rem] tracking-widest text-fixer-dim opacity-0 transition group-hover:opacity-100">
                  {p.guest_id} · {p.likes}♥
                </span>
              </button>
            );
          })}
        </section>
      )}

      {open && (
        <div
          className="fixed inset-0 z-50 flex flex-col justify-end bg-emulsion/95 backdrop-blur"
          onClick={() => setOpen(null)}
        >
          <div className="film-edge bg-card px-5 py-8" onClick={(e) => e.stopPropagation()}>
            <img
              src={photoImageUrl(node ?? "", open.photo_id)}
              alt={`${prettyZone(open.zone)} by ${open.guest_id}`}
              className="mx-auto max-h-[52svh] w-auto object-contain"
            />
            <h2 className="mt-5 text-2xl font-bold capitalize">{prettyZone(open.zone)}</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Picked by {open.guest_id} · {timeOf(open)}
            </p>
            <div className="mt-4 flex gap-3">
              <button
                onClick={() => setOpen(null)}
                className="flex-1 rounded-sm border border-fixer/30 py-2.5 font-mono text-[0.65rem] tracking-widest text-fixer-dim active:scale-95"
              >
                CLOSE
              </button>
              <button
                disabled={!node || downloading}
                onClick={async () => {
                  if (!node) return;
                  setDownloading(true);
                  try {
                    await downloadPhoto(node, open);
                  } finally {
                    setDownloading(false);
                  }
                }}
                className="flex-1 rounded-sm border border-converged bg-converged/20 py-2.5 font-mono text-[0.65rem] font-bold tracking-widest text-converged disabled:opacity-60"
              >
                {downloading ? "SAVING…" : "DOWNLOAD"}
              </button>
            </div>
            <p className="mt-2 text-center font-mono text-[0.5rem] tracking-widest text-fixer-dim">
              WANT TO USE IT IN A STRIP? MAKE A STRIP → THE ROOM, PICK IT FROM THERE.
            </p>
          </div>
        </div>
      )}

      <GuestTabs active="public" />
    </main>
  );
}
