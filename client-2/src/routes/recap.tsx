import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { GuestTabs } from "@/guest/ui";
import { getRecap, photoImageUrl, pickNode, prettyZone, type RecapPhoto } from "@/lib/api";
import { useCurrentEvent } from "@/lib/event";

export const Route = createFileRoute("/recap")({
  head: () => ({
    meta: [
      { title: "Recap — SwarmLens" },
      {
        name: "description",
        content:
          "The most-liked frames of the night, frozen the moment the host called it a wrap — viewable anytime after.",
      },
      { property: "og:title", content: "Recap — SwarmLens" },
      {
        property: "og:description",
        content: "The most-liked frames of the night, frozen the moment the host called it.",
      },
    ],
  }),
  component: Recap,
});

const SLIDE_MS = 4000;

function Recap() {
  const event = useCurrentEvent();
  const [node, setNode] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [photos, setPhotos] = useState<RecapPhoto[]>([]);
  const [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(false);

  // Polls rather than fetching once: a guest may open this tab before the
  // host has ended the event, and it should come alive on its own once
  // the recap fires, the same "no manual refresh needed" pattern as
  // routes/public.tsx and routes/join.$slug.tsx.
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const n = await pickNode();
      if (cancelled) return;
      setNode(n);
      if (!n) return;
      try {
        const r = await getRecap(n, event.event_id);
        if (cancelled) return;
        setReady(r.ready);
        setPhotos(r.photos);
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
  }, [event.event_id]);

  // A snapshot only ever appears once and never changes shape after that
  // (it's frozen server-side), so this only ever fires the first time
  // `ready` flips true -- not on every 5s poll.
  useEffect(() => {
    setIndex(0);
  }, [ready]);

  useEffect(() => {
    if (paused || photos.length < 2) return;
    const id = setInterval(() => setIndex((i) => (i + 1) % photos.length), SLIDE_MS);
    return () => clearInterval(id);
  }, [paused, photos.length]);

  const current = photos[index];

  return (
    <main className="grain min-h-screen pb-24">
      <header className="border-b border-border px-5 pt-8 pb-5">
        <p className="label-mono">RECAP</p>
        <h1 className="mt-2 text-3xl font-extrabold">{event.name}</h1>
        <p className="mt-2 max-w-sm text-sm leading-relaxed text-muted-foreground">
          {ready
            ? "The most-liked frames of the night, frozen the moment the host called it a wrap."
            : "Nothing frozen yet — check back once the host ends the event."}
        </p>
      </header>

      {!ready ? (
        <section className="px-5 py-16 text-center">
          <p className="text-sm text-muted-foreground">
            {node
              ? "The slideshow shows up here on its own once the host wraps the event — no need to refresh."
              : "Waiting for a node to answer…"}
          </p>
        </section>
      ) : photos.length === 0 ? (
        <section className="px-5 py-16 text-center">
          <p className="text-sm text-muted-foreground">
            The host ended the event before anyone liked a photo — nothing to show yet.
          </p>
        </section>
      ) : (
        <section className="px-5 py-6">
          <button
            type="button"
            onClick={() => setPaused((p) => !p)}
            className="film-edge relative block w-full overflow-hidden rounded-sm border border-border bg-card text-left"
          >
            <img
              key={current!.photo_id}
              src={photoImageUrl(node ?? "", current!.photo_id)}
              alt={`${prettyZone(current!.zone)} by ${current!.guest_id}`}
              className="h-[58svh] w-full bg-emulsion object-contain"
            />
            <span className="absolute inset-x-0 bottom-0 bg-emulsion/85 px-4 py-3">
              <span className="block text-sm font-semibold">
                {current!.guest_id} · {prettyZone(current!.zone)}
              </span>
              <span className="block font-mono text-[0.6rem] tracking-widest text-fixer-dim">
                {current!.likes}♥ · #{index + 1} of {photos.length}
                {paused ? " · PAUSED — TAP TO RESUME" : ""}
              </span>
            </span>
          </button>

          <div className="mt-4 flex items-center justify-center gap-4">
            <button
              onClick={() => setIndex((i) => (i - 1 + photos.length) % photos.length)}
              className="rounded-sm border border-border px-4 py-2 font-mono text-xs tracking-widest text-fixer-dim active:scale-95"
            >
              ← PREV
            </button>
            <div className="flex gap-1.5">
              {photos.map((p, i) => (
                <button
                  key={p.photo_id}
                  onClick={() => setIndex(i)}
                  aria-label={`Slide ${i + 1}`}
                  className={`h-1.5 w-1.5 rounded-full ${i === index ? "bg-fixer" : "bg-border"}`}
                />
              ))}
            </div>
            <button
              onClick={() => setIndex((i) => (i + 1) % photos.length)}
              className="rounded-sm border border-border px-4 py-2 font-mono text-xs tracking-widest text-fixer-dim active:scale-95"
            >
              NEXT →
            </button>
          </div>
        </section>
      )}

      <GuestTabs active="recap" />
    </main>
  );
}
