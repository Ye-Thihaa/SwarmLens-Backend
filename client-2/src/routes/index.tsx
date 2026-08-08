import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { events } from "@/guest/data";
import { PerfRail } from "@/guest/ui";
import { CLUSTER_SIZE, getHealth, getPhotos, NODES } from "@/lib/api";

/** APP A — Guest App. No auth, no system internals, one thumb, low light. */
export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "SwarmLens — walk into tonight's room" },
      {
        name: "description",
        content:
          "Pick the event you're standing in. Your phone starts shooting, keeps its own opinion of the room, and shares it with everyone else's.",
      },
      { property: "og:title", content: "SwarmLens — walk into tonight's room" },
      {
        property: "og:description",
        content: "Live events shooting on SwarmLens right now. Enter the one you're in.",
      },
    ],
  }),
  validateSearch: (s: Record<string, unknown>) =>
    typeof s["venue"] === "string" ? { venue: s["venue"] } : {},
  component: Landing,
});

function Landing() {
  const { venue } = Route.useSearch();
  const event = events[0]!;
  const arrived = venue === event.slug ? event : undefined;

  const [guests, setGuests] = useState<number | null>(null);
  const [frames, setFrames] = useState<number | null>(null);
  const [nodesUp, setNodesUp] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const healths = await Promise.allSettled(NODES.map((n) => getHealth(n)));
      const up = healths.filter((h) => h.status === "fulfilled").length;
      const reachable = NODES[healths.findIndex((h) => h.status === "fulfilled")];
      if (!cancelled) setNodesUp(up);
      if (!reachable) return;
      try {
        const photos = await getPhotos(reachable);
        if (cancelled) return;
        setFrames(photos.length);
        setGuests(new Set(photos.map((p) => p.guest_id)).size);
      } catch {
        // stay on the last known-good counts
      }
    }
    void poll();
    const id = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const live = (nodesUp ?? 0) > 0;

  return (
    <main className="grain min-h-screen pb-16">
      <header className="border-b border-border px-5 pt-8 pb-6">
        <div className="flex items-center justify-between">
          <p className="label-mono">SwarmLens · film index 400</p>
          <PerfRail acks={nodesUp ?? 0} total={CLUSTER_SIZE} />
        </div>
        <h1 className="mt-5 text-[2.6rem] leading-[0.95] font-extrabold">
          Every phone here
          <br />
          shoots its own
          <br />
          <span className="text-drifting">contact sheet.</span>
        </h1>
        <p className="mt-4 max-w-sm text-[0.95rem] leading-relaxed text-muted-foreground">
          You shoot. Your phone keeps your frames even with no signal, then hands them around the
          room when it can. What everyone shoots slowly becomes one picture of the night.
        </p>
      </header>

      {arrived && (
        <section className="film-edge border-b border-border bg-emulsion-lift px-5 py-7">
          <p className="label-mono">Door · scanned at the entrance</p>
          <h2 className="mt-2 text-2xl font-bold">{arrived.name}</h2>
          <p className="mt-1 text-sm text-muted-foreground">{arrived.venue}</p>
          <Link
            to="/capture"
            className="mt-5 inline-flex items-center gap-3 rounded-sm bg-fixer px-5 py-3 text-sm font-semibold text-emulsion"
          >
            Walk in and start shooting
            <span className="font-mono text-xs">→</span>
          </Link>
        </section>
      )}

      <section className="px-5 py-7">
        <div className="flex items-baseline justify-between">
          <h2 className="text-lg font-bold">Rooms open now</h2>
          <p className="label-mono">
            {nodesUp === null ? "connecting…" : `${nodesUp}/${CLUSTER_SIZE} nodes up`}
          </p>
        </div>

        {/*
          Core layout: a CONTACT SHEET. This backend has one continuously
          running cluster, not a directory of events, so there is exactly
          one real frame on the strip -- independently exposed, with real
          edge annotations, not averaged into a fake hero.
        */}
        <ul className="mt-4 space-y-4">
          <li>
            <Link
              to="/event/$slug"
              params={{ slug: event.slug }}
              className="group block overflow-hidden rounded-sm border border-border bg-card"
            >
              <div className="relative aspect-[16/10] overflow-hidden">
                <img
                  src={event.hero}
                  alt={`Frame from ${event.name} at ${event.venue}`}
                  width={1024}
                  height={1280}
                  loading="lazy"
                  className="h-full w-full object-cover opacity-80 transition group-hover:opacity-100"
                />
                <div className="absolute inset-x-0 bottom-0 flex items-end justify-between bg-gradient-to-t from-emulsion to-transparent p-3">
                  <span className="font-mono text-[0.65rem] tracking-widest text-fixer-dim">
                    SWARMLENS CLUSTER
                  </span>
                  {live ? (
                    <span className="flex items-center gap-2 font-mono text-[0.65rem] tracking-widest text-converged">
                      <span className="h-1.5 w-1.5 rounded-full bg-converged settling" />
                      LIVE
                    </span>
                  ) : (
                    <span className="font-mono text-[0.65rem] tracking-widest text-stale">
                      UNREACHABLE
                    </span>
                  )}
                </div>
              </div>
              <div className="p-4">
                <h3 className="text-xl font-bold">{event.name}</h3>
                <p className="mt-0.5 text-sm text-muted-foreground">{event.venue}</p>
                <div className="mt-4 flex items-center justify-between border-t border-border pt-3">
                  <p className="font-mono text-[0.7rem] text-fixer-dim">
                    {guests ?? "…"} shooting · {(frames ?? 0).toLocaleString()} frames
                  </p>
                  <p className="font-mono text-[0.7rem] text-drifting">{event.when}</p>
                </div>
              </div>
            </Link>
          </li>
        </ul>
      </section>

      <footer className="px-5 pb-10 text-xs leading-relaxed text-stale">
        No account. Nothing leaves the room until your phone finds a way out.
      </footer>
    </main>
  );
}
