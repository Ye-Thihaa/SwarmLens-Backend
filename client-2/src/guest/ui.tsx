import { Link } from "@tanstack/react-router";

/**
 * Signature motif, guest side: the perf rail.
 * Five perforations = five replicas. Filled = this frame is held there.
 * A guest never sees the word "replica" — they see how much of the film
 * edge has developed.
 */
export function PerfRail({
  acks,
  total = 5,
  lost = 0,
}: {
  acks: number;
  total?: number;
  lost?: number;
}) {
  return (
    <div className="perf-rail" aria-hidden>
      {Array.from({ length: total }).map((_, i) => (
        <span
          key={i}
          className={
            i < acks ? "perf perf-held" : i < acks + lost ? "perf perf-lost" : "perf perf-inflight"
          }
        />
      ))}
    </div>
  );
}

export function GuestTabs({ active }: { active: "capture" | "mine" | "room" | "public" }) {
  const tabs = [
    { key: "capture", to: "/capture", label: "Camera" },
    { key: "mine", to: "/mine", label: "My roll" },
    { key: "room", to: "/event/$slug", label: "The room" },
    { key: "public", to: "/public", label: "Public" },
  ] as const;

  return (
    <nav className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-emulsion/95 backdrop-blur">
      <div className="mx-auto flex max-w-md">
        {tabs.map((t) => (
          <Link
            key={t.key}
            to={t.to}
            {...(t.key === "room" ? { params: { slug: "hollis-marchetti" } } : {})}
            className={`flex-1 py-3.5 text-center text-[0.8rem] font-medium tracking-wide ${
              active === t.key
                ? "text-fixer [box-shadow:inset_0_2px_0_0_var(--drifting)]"
                : "text-stale"
            }`}
          >
            {t.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}
