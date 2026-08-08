import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { GuestTabs, PerfRail } from "@/guest/ui";
import { CLUSTER_SIZE, prettyZone, replicaAcks } from "@/lib/api";
import { currentDeviceId, currentGuestId } from "@/lib/guest";
import { syncOutbox, useOutbox, type OutboxPhoto } from "@/lib/outbox";

export const Route = createFileRoute("/mine")({
  head: () => ({
    meta: [
      { title: "My roll — SwarmLens" },
      {
        name: "description",
        content:
          "Your own frames from tonight, and exactly how far each one has travelled into the room's shared picture.",
      },
      { property: "og:title", content: "My roll — SwarmLens" },
      {
        property: "og:description",
        content: "Frames waiting on the roll, frames now part of the room.",
      },
    ],
  }),
  component: Mine,
});

function Mine() {
  const [who, setWho] = useState<{ guest: string; device: string } | null>(null);
  const [acks, setAcks] = useState<Record<string, number>>({});
  const [syncing, setSyncing] = useState(false);
  const outbox = useOutbox();
  const roll = useMemo(
    () =>
      outbox
        .filter((o): o is OutboxPhoto => o.kind === "photo")
        .sort((a, b) => b.created_at - a.created_at),
    [outbox],
  );

  useEffect(() => {
    setWho({ guest: currentGuestId(), device: currentDeviceId() });
  }, []);

  // Real replica counts: how many of the N configured nodes have already
  // gossiped each of this device's synced photos in. Polled, not
  // computed once, so convergence is visible happening live.
  useEffect(() => {
    const photoIds = roll.filter((p) => p.synced && p.photo_id).map((p) => p.photo_id!);
    if (photoIds.length === 0) return;
    let cancelled = false;
    async function poll() {
      const result = await replicaAcks(photoIds);
      if (!cancelled) setAcks(result);
    }
    void poll();
    const id = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roll.map((p) => p.photo_id).join(",")]);

  const heldCount = roll.filter(
    (p) => p.synced && p.photo_id && (acks[p.photo_id] ?? 0) === CLUSTER_SIZE,
  ).length;

  return (
    <main className="grain min-h-screen pb-24">
      <header className="border-b border-border px-5 pt-8 pb-5">
        <p className="label-mono">{who ? `${who.guest} · ${who.device}` : " "}</p>
        <h1 className="mt-2 text-3xl font-extrabold">Your roll</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {roll.length} frames tonight. {heldCount} {heldCount === 1 ? "has" : "have"} joined the
          room's picture.
        </p>
      </header>

      {roll.length === 0 ? (
        <section className="px-5 py-10 text-center">
          <p className="text-sm text-muted-foreground">
            Nothing shot yet — the camera tab is one tap away.
          </p>
        </section>
      ) : (
        <section className="film-edge overflow-x-auto bg-emulsion-lift py-4 [scrollbar-width:none]">
          <div className="flex gap-3 px-4">
            {roll.map((p) => {
              const ackCount = p.synced && p.photo_id ? (acks[p.photo_id] ?? 0) : 0;
              const held = p.synced && ackCount === CLUSTER_SIZE;
              const url = localBlobUrl(p);
              return (
                <figure
                  key={p.local_id}
                  className={`w-44 shrink-0 rounded-sm border bg-card p-2 ${
                    held ? "border-converged/50" : "border-drifting/40"
                  }`}
                >
                  <img
                    src={url}
                    alt={prettyZone(p.zone)}
                    className={`aspect-[3/4] w-full object-cover ${held ? "" : "settling"}`}
                  />
                  <figcaption className="mt-2">
                    <p className="font-mono text-[0.62rem] tracking-widest text-fixer-dim">
                      {(p.photo_id ?? p.local_id.slice(0, 8)).toUpperCase()} ·{" "}
                      {new Date(p.created_at).toLocaleTimeString("en-GB", { hour12: false })}
                    </p>
                    <p className="mt-0.5 text-[0.78rem] leading-tight font-semibold capitalize">
                      {prettyZone(p.zone)}
                    </p>
                    <div className="mt-2 flex items-center justify-between">
                      <PerfRail acks={p.synced ? Math.max(ackCount, 1) : 0} total={CLUSTER_SIZE} />
                      <span
                        className={`font-mono text-[0.58rem] tracking-widest ${
                          held ? "text-converged" : "text-drifting"
                        }`}
                      >
                        {!p.synced ? "ON THE ROLL" : held ? "IN THE ROOM" : "GOSSIPING…"}
                      </span>
                    </div>
                    {p.error && (
                      <p className="mt-1 font-mono text-[0.55rem] tracking-widest text-safelight">
                        RETRY PENDING
                      </p>
                    )}
                  </figcaption>
                </figure>
              );
            })}
          </div>
        </section>
      )}

      <section className="px-5 py-7">
        <h2 className="text-lg font-bold">What "on the roll" means</h2>
        <p className="mt-2 max-w-sm text-sm leading-relaxed text-muted-foreground">
          A frame on the roll is safe — it's queued on your phone and isn't going anywhere. When
          your phone next reaches one of the three nodes, the frame joins the room's picture; the
          perf rail fills in as each node gossips it further. Nothing you shoot is ever dropped for
          being late.
        </p>
        <button
          disabled={syncing}
          onClick={async () => {
            setSyncing(true);
            await syncOutbox();
            setSyncing(false);
          }}
          className="mt-4 rounded-sm border border-drifting/50 px-4 py-2 font-mono text-[0.6rem] tracking-widest text-drifting disabled:opacity-40"
        >
          {syncing ? "TRYING THE ROOM…" : "TRY THE ROOM NOW"}
        </button>
      </section>

      <GuestTabs active="mine" />
    </main>
  );
}

function localBlobUrl(_p: OutboxPhoto): string {
  // image_base64 is already a data URL's payload -- decode straight from
  // the outbox row itself, since this is always this device's own shot.
  return `data:image/jpeg;base64,${_p.image_base64}`;
}
