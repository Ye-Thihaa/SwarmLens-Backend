import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { pickNode, resolveEvent } from "@/lib/api";
import { joinEvent } from "@/lib/event";

/** Where a scanned QR actually lands. The console prints a URL of the
 * shape /join/{slug}?k={join_token} on each event's card (see
 * routes/console.tsx's QR panel and main.py's GET /events/{slug}) --
 * this route resolves it ONCE against whichever node answers first, stores
 * the result in lib/event.ts, and sends the guest on into the app. Every
 * other guest screen reads the joined event from there, never from this
 * route's params again. */
export const Route = createFileRoute("/join/$slug")({
  validateSearch: (s: Record<string, unknown>) => ({
    k: typeof s["k"] === "string" ? s["k"] : "",
  }),
  head: () => ({ meta: [{ title: "Joining… — SwarmLens" }] }),
  component: Join,
});

function Join() {
  const { slug } = Route.useParams();
  const { k } = Route.useSearch();
  const navigate = useNavigate();
  const [status, setStatus] = useState<"resolving" | "not_found" | "unreachable">("resolving");

  useEffect(() => {
    let cancelled = false;
    async function attempt() {
      const node = await pickNode();
      if (cancelled) return false;
      if (!node) {
        setStatus("unreachable");
        return false;
      }
      const event = await resolveEvent(node, slug, k);
      if (cancelled) return true;
      if (!event) {
        setStatus("not_found");
        return true;
      }
      joinEvent(event);
      void navigate({ to: "/capture" });
      return true;
    }
    void attempt();
    // Retries on a timer rather than giving up after one try: both failure
    // modes here are routine and transient -- no node up yet, or this
    // node hasn't gossiped the event_created row this QR points at -- and
    // a guest standing at the door shouldn't have to manually refresh.
    const id = setInterval(() => void attempt(), 3000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [slug, k, navigate]);

  return (
    <main className="grain flex min-h-screen flex-col items-center justify-center px-6 text-center">
      {status === "resolving" && (
        <>
          <p className="label-mono settling">Walking in</p>
          <p className="mt-2 text-sm text-muted-foreground">Finding your room…</p>
        </>
      )}
      {status === "unreachable" && (
        <>
          <p className="label-mono text-safelight">No node reachable</p>
          <p className="mt-2 max-w-xs text-sm text-muted-foreground">
            Can't reach the cluster right now. Stay on this page — it'll retry the moment a node
            answers.
          </p>
        </>
      )}
      {status === "not_found" && (
        <>
          <p className="label-mono text-safelight">Card not recognised</p>
          <p className="mt-2 max-w-xs text-sm text-muted-foreground">
            This QR doesn't match a room the cluster knows about, or your phone got here before the
            room finished spreading to the node you reached. Try scanning again in a moment.
          </p>
        </>
      )}
    </main>
  );
}
