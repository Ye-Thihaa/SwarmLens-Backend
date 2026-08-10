import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { PerfRail } from "@/guest/ui";
import { CLUSTER_SIZE, getHealth, getPhotos, NODES } from "@/lib/api";
import { useCurrentEvent } from "@/lib/event";

/** APP A — Guest App. No auth, no system internals, one thumb, low light.
 *
 * There is no room DIRECTORY here any more: the cluster hosts several
 * events at once (main.py's "hosted events"), and this app deliberately
 * has no way to list them (see main.py's gated GET /events) -- a guest
 * arrives at exactly one, by scanning that event's own QR, which routes
 * through /join/$slug and stores the result in lib/event.ts. This page is
 * what a guest sees if they land on the bare domain without having
 * scanned anything yet: whichever event this phone joined last, or the
 * single-event demo room if it never joined one at all. */
export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "SwarmLens — walk into tonight's room" },
      {
        name: "description",
        content:
          "Scan the card on your table. Your phone starts shooting, keeps its own opinion of the room, and shares it with everyone else's.",
      },
      { property: "og:title", content: "SwarmLens — walk into tonight's room" },
      {
        property: "og:description",
        content: "Scan the QR at your event to walk in and start shooting.",
      },
    ],
  }),
  component: Landing,
});

function Landing() {
  const event = useCurrentEvent();

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
        const photos = await getPhotos(reachable, event.event_id);
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
  }, [event.event_id]);

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

      <section className="px-5 py-7">
        <div className="flex items-baseline justify-between">
          <h2 className="text-lg font-bold">Your room</h2>
          <p className="label-mono">
            {nodesUp === null ? "connecting…" : `${nodesUp}/${CLUSTER_SIZE} nodes up`}
          </p>
        </div>

        <ul className="mt-4 space-y-4">
          <li>
            <Link
              to="/event/$slug"
              params={{ slug: event.slug }}
              className="group block overflow-hidden rounded-sm border border-border bg-card"
            >
              <div className="relative aspect-[16/10] overflow-hidden bg-emulsion-lift">
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

        <Link
          to="/capture"
          className="mt-5 inline-flex items-center gap-3 rounded-sm bg-fixer px-5 py-3 text-sm font-semibold text-emulsion"
        >
          Walk in and start shooting
          <span className="font-mono text-xs">→</span>
        </Link>
      </section>

      <footer className="px-5 pb-10 text-xs leading-relaxed text-stale">
        No account. Nothing leaves the room until your phone finds a way out.
      </footer>
    </main>
  );
}
