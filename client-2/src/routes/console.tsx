import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import type { NodeState } from "@/operator/cluster";
import {
  CLUSTER,
  URL_TO_ID,
  getHealth,
  getPhotos,
  getRecap,
  getZones,
  getZonesQuorum,
  prettyZone,
  RECAP_TOP_N,
  type HostedEventAdmin,
  type NodeHealth,
  type QuorumResult,
  type Recap,
  type ZoneScore,
} from "@/lib/api";
import { checkConsoleSession, loginToConsole, logoutOfConsole } from "@/lib/consoleAuth";
import {
  serverCreateEvent,
  serverHealAll,
  serverIsolateNode,
  serverEventToken,
  serverListEvents,
  serverTriggerRecap,
} from "@/lib/operatorGateway";
import { joinQrSvg, printJoinCard } from "@/lib/qr";

/** APP B — Operator Console. One operator, laptop → room display. No camera.
 * Real server-side gate (see lib/consoleAuth.ts): the loader checks the
 * session cookie on every request, including the first SSR render, so an
 * unauthenticated visitor never receives the console's data. */
export const Route = createFileRoute("/console")({
  head: () => ({
    meta: [
      { title: "SwarmLens Operator Console" },
      {
        name: "description",
        content:
          "Live cluster state behind SwarmLens: Raft leader and term, gossip convergence, W/R/N quorum, and a chaos panel for partitioning the mesh.",
      },
      { property: "og:title", content: "SwarmLens Operator Console" },
      {
        property: "og:description",
        content: "Raft term, gossip convergence and quorum state for the room, on one screen.",
      },
    ],
  }),
  loader: async () => {
    const { authorized } = await checkConsoleSession();
    return { authorized };
  },
  component: Console,
});

type Log = { t: string; text: string; kind: "ok" | "warn" | "bad" };
type HealthMap = Record<string, NodeHealth | null>;

function nodeState(h: NodeHealth | null): NodeState {
  if (!h) return "dead";
  if (h.raft.role === "leader") return "leader";
  if (h.raft.role === "candidate") return "candidate";
  if (h.gossip.partitioned.length > 0) return "partitioned";
  return "follower";
}

function secondsSinceLastSync(h: NodeHealth): number | null {
  const times = Object.values(h.gossip.peers)
    .map((p) => p.last_ok)
    .filter((t): t is number => t != null);
  if (times.length === 0) return null;
  return Math.max(0, Date.now() / 1000 - Math.max(...times));
}

function Console() {
  const { authorized } = Route.useLoaderData();
  const [unlocked, setUnlocked] = useState(authorized);
  const [key, setKey] = useState("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const [health, setHealth] = useState<HealthMap>({});
  const [zones, setZones] = useState<ZoneScore[]>([]);
  const [frames, setFrames] = useState(0);
  const [guests, setGuests] = useState(0);

  const [w, setW] = useState(3);
  const [r, setR] = useState(3);
  const [quorum, setQuorum] = useState<QuorumResult | null>(null);
  const [quorumRunning, setQuorumRunning] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const [log, setLog] = useState<Log[]>([]);
  const [latched, setLatched] = useState(false);
  const prevRef = useRef<
    Record<string, { term: number; leader: string | null; partitioned: string[] }>
  >({});

  // ---- hosted events: the multi-tenant directory this console alone can see ----
  const [events, setEvents] = useState<HostedEventAdmin[]>([]);
  const [eventsTotal, setEventsTotal] = useState(0);
  const [eventsLoaded, setEventsLoaded] = useState(false);
  const [tokenBySlug, setTokenBySlug] = useState<Record<string, string>>({});
  // undefined = every event merged -- the same cluster-wide view this
  // console always showed before multi-event hosting existed. Selecting a
  // real event scopes the room stats, the aesthetic map and the quorum
  // panel below to that one event, so "which room is this reading?" is
  // never ambiguous once more than one exists.
  const [selectedEventId, setSelectedEventId] = useState<string | undefined>(undefined);

  const [newSlug, setNewSlug] = useState("");
  const [newName, setNewName] = useState("");
  const [newVenue, setNewVenue] = useState("");
  const [newZones, setNewZones] = useState("");
  const [creatingEvent, setCreatingEvent] = useState(false);
  const [createEventError, setCreateEventError] = useState<string | null>(null);

  // Where a printed QR should actually point. Defaults to this page's own
  // origin, which is exactly wrong for a guest's phone whenever the
  // console itself was opened at localhost/127.0.0.1 -- see the warning
  // rendered below. Persisted so an operator only has to correct it once
  // per venue, not once per event card.
  const [publicBaseUrl, setPublicBaseUrl] = useState("");
  useEffect(() => {
    const saved = localStorage.getItem("swarmlens_public_base_url");
    setPublicBaseUrl(saved || window.location.origin);
  }, []);
  useEffect(() => {
    if (publicBaseUrl) localStorage.setItem("swarmlens_public_base_url", publicBaseUrl);
  }, [publicBaseUrl]);
  const baseUrlIsLocal = /^https?:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(publicBaseUrl);

  const [qrOpenId, setQrOpenId] = useState<string | null>(null);
  const [qrSvgById, setQrSvgById] = useState<Record<string, string>>({});

  // Recap: an operator's "end this event" action, freezing the top-liked
  // slideshow guests see afterward on /recap. Fetched lazily per event
  // (a button, not a poll) -- there's no cluster-wide urgency to knowing
  // this the moment it changes, unlike the health/zone panels below.
  const [recapByEvent, setRecapByEvent] = useState<Record<string, Recap>>({});
  const [recapBusyId, setRecapBusyId] = useState<string | null>(null);

  async function loadEvents() {
    const page = await serverListEvents();
    setEvents(page.events);
    setEventsTotal(page.total);
    setEventsLoaded(true);
  }

  // Join tokens are no longer in the directory response (see api.ts's
  // HostedEventAdmin) -- fetched per event, only when an operator actually
  // reaches for that event's link or QR, and cached for the session.
  async function tokenFor(ev: HostedEventAdmin): Promise<string> {
    const cached = tokenBySlug[ev.slug];
    if (cached) return cached;
    const { join_token } = await serverEventToken({ data: { slug: ev.slug } });
    setTokenBySlug((m) => ({ ...m, [ev.slug]: join_token }));
    return join_token;
  }

  async function checkRecap(ev: HostedEventAdmin) {
    const n = CLUSTER.find((c) => health[c.id])?.url ?? CLUSTER[0]!.url;
    try {
      const r = await getRecap(n, ev.event_id);
      setRecapByEvent((m) => ({ ...m, [ev.event_id]: r }));
    } catch {
      // stay on the last known-good read
    }
  }

  async function freezeRecap(ev: HostedEventAdmin) {
    setRecapBusyId(ev.event_id);
    try {
      const res = await serverTriggerRecap({ data: { eventId: ev.event_id } });
      if (!res.ok) {
        push(`couldn't reach any node to freeze ${ev.name}'s recap`, "bad");
        return;
      }
      await checkRecap(ev);
      push(`recap frozen for ${ev.name} · top ${RECAP_TOP_N} most-liked`, "ok");
    } finally {
      setRecapBusyId(null);
    }
  }

  useEffect(() => {
    if (!unlocked) return;
    void loadEvents();
    const id = setInterval(() => void loadEvents(), 5000);
    return () => clearInterval(id);
  }, [unlocked]);

  async function createEvent(e: FormEvent) {
    e.preventDefault();
    setCreateEventError(null);
    setCreatingEvent(true);
    try {
      const zones = newZones
        .split(",")
        .map((z) => z.trim().toLowerCase().replace(/\s+/g, "_"))
        .filter(Boolean);
      const res = await serverCreateEvent({
        data: { slug: newSlug.trim().toLowerCase(), name: newName.trim(), venue: newVenue.trim(), when: "", zones },
      });
      if (!res.ok) {
        setCreateEventError(
          res.reason === "slug_taken"
            ? `The slug "${newSlug}" is already in use by another event.`
            : res.reason === "invalid"
              ? "Needs at least a slug (letters, digits, dashes) and a name."
              : "Couldn't reach the cluster to create the event.",
        );
        return;
      }
      setNewSlug("");
      setNewName("");
      setNewVenue("");
      setNewZones("");
      await loadEvents();
      setSelectedEventId(res.event.event_id);
      push(`event created: ${res.event.name} (${res.event.slug})`, "ok");
    } finally {
      setCreatingEvent(false);
    }
  }

  async function toggleQr(ev: HostedEventAdmin) {
    if (qrOpenId === ev.event_id) {
      setQrOpenId(null);
      return;
    }
    setQrOpenId(ev.event_id);
    if (!qrSvgById[ev.event_id]) {
      const url = await joinUrl(ev);
      const svg = await joinQrSvg(url);
      setQrSvgById((m) => ({ ...m, [ev.event_id]: svg }));
    }
  }

  async function joinUrl(ev: HostedEventAdmin): Promise<string> {
    const token = await tokenFor(ev);
    return `${publicBaseUrl || window.location.origin}/join/${ev.slug}?k=${token}`;
  }

  async function copyJoinUrl(ev: HostedEventAdmin) {
    await navigator.clipboard.writeText(await joinUrl(ev));
    push(`copied join link for ${ev.name}`, "ok");
  }

  const selectedEvent = useMemo(
    () => events.find((e) => e.event_id === selectedEventId),
    [events, selectedEventId],
  );

  function push(text: string, kind: Log["kind"]) {
    const t = new Date().toLocaleTimeString("en-GB", { hour12: false });
    setLog((l) => [{ t, text, kind }, ...l].slice(0, 8));
  }

  // Real state, polled at dashboard.html's cadence (1s). Diffs against
  // the previous snapshot to produce a real event tape -- term/leader
  // changes and partition set changes, not scripted lines.
  useEffect(() => {
    if (!unlocked) return;
    let cancelled = false;

    async function poll() {
      const results = await Promise.all(
        CLUSTER.map(async (n) => {
          try {
            return [n.id, await getHealth(n.url)] as const;
          } catch {
            return [n.id, null] as const;
          }
        }),
      );
      if (cancelled) return;
      const next: HealthMap = Object.fromEntries(results);
      diffAndLog(next);
      setHealth(next);

      const anyNode = CLUSTER.find((n) => next[n.id])?.url;
      if (anyNode) {
        try {
          const [ps, zs] = await Promise.all([
            getPhotos(anyNode, selectedEventId),
            getZones(anyNode, selectedEventId),
          ]);
          if (!cancelled) {
            setFrames(ps.length);
            setGuests(new Set(ps.map((p) => p.guest_id)).size);
            setZones(zs);
          }
        } catch {
          // stay on the last known-good snapshot
        }
      }
    }

    function diffAndLog(next: HealthMap) {
      const prev = prevRef.current;
      for (const n of CLUSTER) {
        const h = next[n.id];
        const p = prev[n.id];
        if (!h) {
          if (p) push(`${n.id} unreachable`, "bad");
          delete prev[n.id];
          continue;
        }
        if (
          p &&
          h.raft.role === "leader" &&
          (h.raft.term !== p.term || h.raft.leader_id !== p.leader)
        ) {
          push(`term ${h.raft.term} · ${n.id} elected leader`, "ok");
        }
        if (p) {
          for (const url of h.gossip.partitioned) {
            if (!p.partitioned.includes(url)) push(`${n.id} cut → ${URL_TO_ID[url] ?? url}`, "bad");
          }
          for (const url of p.partitioned) {
            if (!h.gossip.partitioned.includes(url))
              push(`${n.id} healed → ${URL_TO_ID[url] ?? url}`, "warn");
          }
        }
        prev[n.id] = {
          term: h.raft.term,
          leader: h.raft.leader_id,
          partitioned: h.gossip.partitioned,
        };
      }
    }

    void poll();
    const id = setInterval(poll, 1000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [unlocked, selectedEventId]);

  const healthy = CLUSTER.map((n) => health[n.id]).filter((h): h is NodeHealth => !!h);
  const leaderIds = new Set(healthy.map((h) => h.raft.leader_id).filter((x): x is string => !!x));
  const anyPartitioned = healthy.some((h) => h.gossip.partitioned.length > 0);
  const anyCandidate = healthy.some((h) => h.raft.role === "candidate");
  const converged =
    healthy.length === CLUSTER.length && leaderIds.size === 1 && !anyPartitioned && !anyCandidate;
  const term = healthy.length > 0 ? Math.max(...healthy.map((h) => h.raft.term)) : 0;
  const leaderNode = CLUSTER.find((n) => health[n.id]?.raft.role === "leader");
  const topZone = zones[0];

  // agreement latch: re-arms whenever the cluster comes back into agreement
  useEffect(() => {
    if (converged) {
      setLatched(false);
      const id = setTimeout(() => setLatched(true), 40);
      return () => clearTimeout(id);
    }
    setLatched(false);
    return;
  }, [converged, term]);

  async function isolateLeader() {
    if (!leaderNode) return;
    setBusy("isolate-leader");
    push(`isolating ${leaderNode.id} from gossip…`, "warn");
    await serverIsolateNode({ data: { nodeId: leaderNode.id } });
    push(
      `${leaderNode.id} cut from every peer · raft heartbeats bypass this, no re-election`,
      "bad",
    );
    setBusy(null);
  }

  async function isolateOne(nodeId: string) {
    setBusy(`isolate-${nodeId}`);
    await serverIsolateNode({ data: { nodeId } });
    push(`${nodeId} cut from every peer`, "bad");
    setBusy(null);
  }

  async function healAll() {
    setBusy("heal");
    await serverHealAll();
    push("heal requested on every node · partitions clearing", "warn");
    setBusy(null);
  }

  async function runQuorumRead() {
    setQuorumRunning(true);
    try {
      const result = await getZonesQuorum(CLUSTER[0]!.url, r, w, selectedEventId);
      setQuorum(result);
      const unreachableNote =
        result.unreachable.length > 0
          ? ` · missed ${result.unreachable.map((u) => URL_TO_ID[u] ?? u).join(", ")}`
          : "";
      push(
        `quorum read R=${result.R} W=${result.W} · queried ${result.queried.length}/${result.N}${unreachableNote}`,
        result.strongly_consistent ? "ok" : "warn",
      );
    } catch (e) {
      push(`quorum read failed: ${String(e)}`, "bad");
    }
    setQuorumRunning(false);
  }

  async function reset() {
    setBusy("reset");
    await serverHealAll();
    setW(3);
    setR(3);
    setQuorum(null);
    push("cluster reset · every partition healed", "ok");
    setBusy(null);
  }

  if (!unlocked) {
    return (
      <main className="grain flex min-h-screen items-center justify-center px-6">
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setAuthError(null);
            setChecking(true);
            try {
              await loginToConsole({ data: { password: key } });
              setUnlocked(true);
            } catch {
              setAuthError("Incorrect password.");
            }
            setChecking(false);
          }}
          className="w-full max-w-sm rounded-sm border border-border bg-card p-6"
        >
          <p className="label-mono">SwarmLens · operator</p>
          <h1 className="mt-2 text-2xl font-extrabold">Operator key</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            This console is not the guest app. It can partition nodes from the cluster.
          </p>
          <input
            value={key}
            onChange={(e) => setKey(e.target.value)}
            type="password"
            placeholder="operator key"
            autoFocus
            className="mt-5 w-full rounded-sm border border-input bg-emulsion px-3 py-2.5 font-mono text-sm"
          />
          {authError && <p className="mt-2 text-sm text-safelight">{authError}</p>}
          <button
            disabled={checking}
            className="mt-4 w-full rounded-sm bg-fixer py-2.5 text-sm font-semibold text-emulsion disabled:opacity-50"
          >
            {checking ? "Checking…" : "Open console"}
          </button>
        </form>
      </main>
    );
  }

  return (
    <main className="grain min-h-screen px-6 py-6 xl:px-10">
      <header className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-4 border-b border-border pb-4 lg:flex lg:justify-between">
        <div className="min-w-0">
          <p className="label-mono">SwarmLens · 3-node local cluster</p>
          <h1 className="truncate text-2xl font-extrabold xl:text-3xl">Operator Console</h1>
        </div>
        <div className="flex shrink-0 items-center gap-6 font-mono text-xs">
          <button
            onClick={async () => {
              await logoutOfConsole();
              setUnlocked(false);
              setKey("");
            }}
            className="text-fixer-dim underline decoration-dotted underline-offset-2"
          >
            log out
          </button>
          <span className="text-fixer-dim">
            TERM <span className="text-fixer">{term}</span>
          </span>
          <span className="text-fixer-dim">
            W <span className="text-fixer">{w}</span> / R <span className="text-fixer">{r}</span> /
            N <span className="text-fixer">{CLUSTER.length}</span>
          </span>
        </div>
      </header>

      {/* SIGNATURE: the agreement latch — one wide perf rail across the top.
          Amber and open while replicas disagree; snaps green and stamped when
          the cluster agrees. Readable from across the room in under a second. */}
      <section
        className={`mt-5 rounded-sm border-2 p-5 ${
          converged ? "border-converged bg-converged/10" : "border-safelight bg-safelight/10"
        } ${converged && latched ? "latch" : ""}`}
      >
        <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-6">
          <div className="min-w-0">
            <p className="label-mono">Agreement latch</p>
            <p
              className={`mt-1 font-display text-3xl font-extrabold xl:text-5xl ${
                converged ? "text-converged" : "text-safelight"
              }`}
            >
              {converged
                ? `AGREED · TERM ${term}`
                : anyPartitioned
                  ? "SPLIT · GOSSIP CUT SOMEWHERE"
                  : anyCandidate
                    ? "ELECTING · NO LEADER"
                    : "DISAGREEING"}
            </p>
          </div>
          <div className="perf-rail shrink-0">
            {CLUSTER.map((n) => {
              const state = nodeState(health[n.id] ?? null);
              return (
                <span
                  key={n.id}
                  title={n.id}
                  className={`perf h-10 w-6 ${
                    state === "dead" || state === "partitioned"
                      ? "perf-lost"
                      : converged
                        ? "perf-held"
                        : "perf-inflight"
                  }`}
                />
              );
            })}
          </div>
        </div>
      </section>

      {/* Hosted events: this cluster's multi-tenant directory. Every panel
          below (room stats, aesthetic map, quorum) reads whichever event
          is selected here -- undefined ("All events") merges every
          event's data, which is the only view that ever existed before
          hosted events did. */}
      <section className="mt-5 rounded-sm border border-border bg-card p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-lg font-bold">
            Hosted events
            {eventsTotal > events.length && (
              <span className="ml-2 font-mono text-[0.6rem] tracking-widest text-fixer-dim">
                SHOWING {events.length} OF {eventsTotal}
              </span>
            )}
          </h2>
          <label className="flex items-center gap-2 font-mono text-xs text-fixer-dim">
            VIEWING
            <select
              value={selectedEventId ?? ""}
              onChange={(e) => setSelectedEventId(e.target.value || undefined)}
              className="rounded-sm border border-input bg-emulsion px-2 py-1 text-xs"
            >
              <option value="">All events (merged)</option>
              {events.map((ev) => (
                <option key={ev.event_id} value={ev.event_id}>
                  {ev.name} ({ev.slug})
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* Where a scanned QR should send a phone. Defaults to this
            page's own origin, which is exactly wrong if this console was
            opened at localhost/127.0.0.1 -- a phone scanning that QR has
            no idea what "localhost" means on its own network. */}
        <div className="mt-3">
          <label className="block font-mono text-[0.65rem] tracking-widest text-fixer-dim">
            PUBLIC URL FOR PRINTED QR CODES
          </label>
          <input
            value={publicBaseUrl}
            onChange={(e) => setPublicBaseUrl(e.target.value)}
            placeholder="https://your-venue-domain.example"
            className="mt-1 w-full max-w-md rounded-sm border border-input bg-emulsion px-3 py-1.5 font-mono text-xs"
          />
          {baseUrlIsLocal && (
            <p className="mt-1 max-w-md text-[0.7rem] leading-relaxed text-safelight">
              This points at {publicBaseUrl || "localhost"}, which only means something on THIS
              machine. A guest's phone needs the venue's real LAN IP or public domain here before
              you print anything.
            </p>
          )}
        </div>

        <form onSubmit={createEvent} className="mt-4 grid gap-2 border-t border-border pt-4 sm:grid-cols-2">
          <input
            value={newSlug}
            onChange={(e) => setNewSlug(e.target.value)}
            placeholder="slug (e.g. hollis-marchetti)"
            required
            className="rounded-sm border border-input bg-emulsion px-3 py-2 font-mono text-xs"
          />
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Event name"
            required
            className="rounded-sm border border-input bg-emulsion px-3 py-2 text-sm"
          />
          <input
            value={newVenue}
            onChange={(e) => setNewVenue(e.target.value)}
            placeholder="Venue (optional)"
            className="rounded-sm border border-input bg-emulsion px-3 py-2 text-sm"
          />
          <input
            value={newZones}
            onChange={(e) => setNewZones(e.target.value)}
            placeholder="Zones, comma-separated (blank = single room, no picker)"
            className="rounded-sm border border-input bg-emulsion px-3 py-2 font-mono text-xs"
          />
          <div className="sm:col-span-2">
            {createEventError && (
              <p className="mb-2 text-xs text-safelight">{createEventError}</p>
            )}
            <button
              disabled={creatingEvent}
              className="rounded-sm bg-fixer px-4 py-2 text-sm font-semibold text-emulsion disabled:opacity-50"
            >
              {creatingEvent ? "Creating…" : "Create event"}
            </button>
          </div>
        </form>

        <ul className="mt-4 space-y-2 border-t border-border pt-4">
          {!eventsLoaded && (
            <li className="font-mono text-[0.62rem] text-stale">loading events…</li>
          )}
          {eventsLoaded && events.length === 0 && (
            <li className="font-mono text-[0.62rem] tracking-widest text-stale">
              NO EVENTS HOSTED YET. CREATE ONE ABOVE.
            </li>
          )}
          {events.map((ev) => (
            <li key={ev.event_id} className="rounded-sm border border-border p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold">{ev.name}</p>
                  <p className="font-mono text-[0.6rem] tracking-widest text-fixer-dim">
                    <span className={ev.status === "ended" ? "text-stale" : "text-converged"}>
                      {ev.status === "ended" ? "ENDED" : "ACTIVE"}
                    </span>
                    {" · "}/{ev.slug} ·{" "}
                    {ev.zones.length > 1 ? `${ev.zones.length} zones` : "single room"}
                    {ev.venue ? ` · ${ev.venue}` : ""}
                  </p>
                </div>
                <div className="flex shrink-0 gap-2">
                  <button
                    onClick={() => setSelectedEventId(ev.event_id)}
                    className={`rounded-sm border px-2 py-1 font-mono text-[0.6rem] tracking-widest ${
                      selectedEventId === ev.event_id
                        ? "border-drifting text-drifting"
                        : "border-border text-fixer-dim"
                    }`}
                  >
                    VIEW
                  </button>
                  <button
                    onClick={() => void copyJoinUrl(ev)}
                    className="rounded-sm border border-border px-2 py-1 font-mono text-[0.6rem] tracking-widest text-fixer-dim"
                  >
                    COPY LINK
                  </button>
                  <button
                    onClick={() => void toggleQr(ev)}
                    className="rounded-sm border border-border px-2 py-1 font-mono text-[0.6rem] tracking-widest text-fixer-dim"
                  >
                    {qrOpenId === ev.event_id ? "HIDE QR" : "SHOW QR"}
                  </button>
                  <button
                    onClick={() => void freezeRecap(ev)}
                    disabled={recapBusyId === ev.event_id}
                    className="rounded-sm border border-drifting px-2 py-1 font-mono text-[0.6rem] tracking-widest text-drifting disabled:opacity-40"
                  >
                    {recapBusyId === ev.event_id
                      ? "FREEZING…"
                      : recapByEvent[ev.event_id]?.ready
                        ? "RE-CHECK RECAP"
                        : "END EVENT & FREEZE RECAP"}
                  </button>
                </div>
              </div>
              {recapByEvent[ev.event_id] && (
                <p className="mt-2 font-mono text-[0.6rem] tracking-widest text-fixer-dim">
                  {recapByEvent[ev.event_id]!.ready
                    ? `RECAP READY · ${recapByEvent[ev.event_id]!.photos.length} PHOTO${
                        recapByEvent[ev.event_id]!.photos.length === 1 ? "" : "S"
                      } FROZEN`
                    : "NOT FROZEN YET"}
                </p>
              )}
              {qrOpenId === ev.event_id && (
                <div className="mt-3 flex flex-wrap items-center gap-4 border-t border-border pt-3">
                  {qrSvgById[ev.event_id] ? (
                    <div
                      className="h-32 w-32 shrink-0 bg-white p-1.5"
                      // qrcode's toString(svg) output is our own generated
                      // markup, not user input -- nothing here ever comes
                      // from a guest or another operator.
                      dangerouslySetInnerHTML={{ __html: qrSvgById[ev.event_id]! }}
                    />
                  ) : (
                    <p className="font-mono text-[0.6rem] text-stale">rendering…</p>
                  )}
                  <div className="min-w-0 flex-1 space-y-2">
                    {/* Rendered from the cached token rather than by calling
                        joinUrl (now async, since tokens are fetched on
                        demand) -- toggleQr has already resolved it by the
                        time this panel is open. */}
                    <p className="break-all font-mono text-[0.6rem] text-fixer-dim">
                      {tokenBySlug[ev.slug]
                        ? `${publicBaseUrl || ""}/join/${ev.slug}?k=${tokenBySlug[ev.slug]}`
                        : "fetching join link…"}
                    </p>
                    <button
                      disabled={!qrSvgById[ev.event_id]}
                      onClick={() =>
                        qrSvgById[ev.event_id] &&
                        printJoinCard(qrSvgById[ev.event_id]!, ev.name, ev.venue)
                      }
                      className="rounded-sm border border-border px-3 py-1.5 font-mono text-[0.6rem] tracking-widest disabled:opacity-40"
                    >
                      PRINT TABLE CARD
                    </button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      </section>

      <div className="mt-5 grid gap-5 lg:grid-cols-[2fr_1fr]">
        <div className="space-y-5">
          <p className="label-mono">
            {selectedEvent ? `Reading: ${selectedEvent.name}` : "Reading: every hosted event, merged"}
          </p>

          {/* room state, reframed as system state */}
          <section className="grid grid-cols-2 gap-3 xl:grid-cols-4">
            {[
              ["Guests shooting", String(guests), "text-fixer"],
              ["Frames landed", frames.toLocaleString(), "text-fixer"],
              ["Leading spot", topZone ? prettyZone(topZone.zone) : "—", "text-drifting"],
              [
                "Last quorum read",
                quorum
                  ? quorum.strongly_consistent
                    ? "consistent"
                    : "may be stale"
                  : "not run yet",
                quorum
                  ? quorum.strongly_consistent
                    ? "text-converged"
                    : "text-safelight"
                  : "text-stale",
              ],
            ].map(([label, value, cls]) => (
              <div key={label} className="rounded-sm border border-border bg-card p-4">
                <p className="label-mono">{label}</p>
                <p
                  className={`mt-1.5 font-display text-xl leading-tight font-bold xl:text-2xl ${cls}`}
                >
                  {value}
                </p>
              </div>
            ))}
          </section>

          {/* the mesh, drawn as a contact sheet of nodes */}
          <section className="rounded-sm border border-border bg-card p-4">
            <div className="flex items-baseline justify-between">
              <h2 className="text-lg font-bold">Mesh</h2>
              <p className="label-mono">gossip ~every 1s</p>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3">
              {CLUSTER.map((n) => {
                const h = health[n.id] ?? null;
                const state = nodeState(h);
                const bad = state === "dead" || state === "partitioned";
                const sinceSync = h ? secondsSinceLastSync(h) : null;
                return (
                  <div
                    key={n.id}
                    className={`film-edge rounded-sm border px-3 py-5 text-center ${
                      state === "leader"
                        ? "border-converged bg-converged/10"
                        : bad
                          ? "border-safelight bg-safelight/10"
                          : state === "candidate"
                            ? "border-drifting bg-drifting/10 settling"
                            : "border-border"
                    }`}
                  >
                    <p className="font-mono text-sm font-bold">{n.id}</p>
                    <p
                      className={`mt-1 font-mono text-[0.6rem] tracking-widest ${
                        state === "leader"
                          ? "text-converged"
                          : bad
                            ? "text-safelight"
                            : state === "candidate"
                              ? "text-drifting"
                              : "text-fixer-dim"
                      }`}
                    >
                      {state.toUpperCase()}
                    </p>
                    <p className="mt-2 font-mono text-[0.6rem] text-stale">
                      {!h ? "—" : `${h.events} events`}
                    </p>
                    <p className="mt-0.5 font-mono text-[0.55rem] text-stale">
                      {sinceSync != null ? `synced ${sinceSync.toFixed(1)}s ago` : ""}
                    </p>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="rounded-sm border border-border bg-card p-4">
            <h2 className="text-lg font-bold">Merged aesthetic scores</h2>
            {zones.length === 0 ? (
              <p className="mt-3 font-mono text-[0.6rem] tracking-widest text-stale">
                NO FRAMES YET
              </p>
            ) : (
              <ul className="mt-3 space-y-2">
                {zones.map((z) => (
                  <li key={z.zone} className="grid grid-cols-[12rem_1fr_5rem] items-center gap-3">
                    <span className="truncate text-sm capitalize">{prettyZone(z.zone)}</span>
                    <span className="h-2 bg-muted">
                      <span
                        className="block h-full"
                        style={{
                          width: `${Math.min(100, (z.likes * 2 + z.photos) * 8)}%`,
                          background: z.stale ? "var(--safelight)" : "var(--converged)",
                        }}
                      />
                    </span>
                    <span className="text-right font-mono text-xs text-fixer-dim">
                      {z.avg_aesthetic != null ? z.avg_aesthetic.toFixed(2) : "—"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {quorum && (
            <section className="rounded-sm border border-border bg-card p-4">
              <h2 className="text-lg font-bold">Last quorum read</h2>
              <p className="mt-1 font-mono text-[0.65rem] tracking-widest text-fixer-dim">
                N={quorum.N} R={quorum.R} W={quorum.W} ·{" "}
                {quorum.strongly_consistent ? "R+W>N, always fresh" : "R+W≤N, can land stale"}
              </p>
              <p className="mt-2 font-mono text-[0.6rem] tracking-widest text-fixer-dim">
                queried: {quorum.queried.map((u) => URL_TO_ID[u] ?? u).join(", ") || "—"}
              </p>
              <p className="mt-1 font-mono text-[0.6rem] tracking-widest text-safelight">
                unreachable: {quorum.unreachable.map((u) => URL_TO_ID[u] ?? u).join(", ") || "none"}
              </p>
            </section>
          )}
        </div>

        {/* chaos panel */}
        <aside className="space-y-5">
          <section className="rounded-sm border-2 border-safelight/60 bg-card p-4">
            <p className="label-mono">Chaos</p>
            <h2 className="mt-1 text-lg font-bold text-safelight">Break it on purpose</h2>
            <div className="mt-4 space-y-2">
              <button
                onClick={isolateLeader}
                disabled={!leaderNode || busy === "isolate-leader"}
                className="w-full rounded-sm border border-safelight px-3 py-3 text-left text-sm font-semibold text-safelight disabled:opacity-40"
              >
                Isolate the leader
                <span className="block font-mono text-[0.6rem] tracking-widest text-fixer-dim">
                  CUTS GOSSIP BOTH WAYS · RAFT HEARTBEATS UNAFFECTED, NO RE-ELECTION
                </span>
              </button>

              <div className="flex gap-2">
                {CLUSTER.map((n) => (
                  <button
                    key={n.id}
                    onClick={() => isolateOne(n.id)}
                    disabled={busy === `isolate-${n.id}`}
                    className="flex-1 rounded-sm border border-drifting px-2 py-2 font-mono text-[0.6rem] tracking-widest text-drifting disabled:opacity-40"
                  >
                    CUT {n.id}
                  </button>
                ))}
              </div>

              <button
                onClick={healAll}
                disabled={busy === "heal"}
                className="w-full rounded-sm border border-converged px-3 py-3 text-left text-sm font-semibold text-converged disabled:opacity-40"
              >
                Heal all
                <span className="block font-mono text-[0.6rem] tracking-widest text-fixer-dim">
                  CLEAR EVERY PARTITION ON EVERY NODE
                </span>
              </button>

              <div className="flex gap-2 pt-2">
                {[1, 2, 3].map((v) => (
                  <button
                    key={v}
                    onClick={() => setW(v)}
                    className={`flex-1 rounded-sm border py-2 font-mono text-xs ${
                      w === v ? "border-drifting text-drifting" : "border-border text-fixer-dim"
                    }`}
                  >
                    W={v}
                  </button>
                ))}
              </div>
              <div className="flex gap-2">
                {[1, 2, 3].map((v) => (
                  <button
                    key={v}
                    onClick={() => setR(v)}
                    className={`flex-1 rounded-sm border py-2 font-mono text-xs ${
                      r === v ? "border-drifting text-drifting" : "border-border text-fixer-dim"
                    }`}
                  >
                    R={v}
                  </button>
                ))}
              </div>
              <button
                onClick={runQuorumRead}
                disabled={quorumRunning}
                className="w-full rounded-sm border border-border px-3 py-3 text-left text-sm font-semibold disabled:opacity-40"
              >
                {quorumRunning ? "Reading…" : "Run a quorum read"}
                <span className="block font-mono text-[0.6rem] tracking-widest text-fixer-dim">
                  GET /zones/quorum?R={r}&W={w} · SAMPLE IS RANDOM EACH CALL
                </span>
              </button>
              <button
                onClick={reset}
                disabled={busy === "reset"}
                className="w-full rounded-sm bg-fixer py-2.5 text-sm font-semibold text-emulsion disabled:opacity-40"
              >
                Reset cluster
              </button>
            </div>
          </section>

          <section className="rounded-sm border border-border bg-card p-4">
            <p className="label-mono">Event tape</p>
            <ul className="mt-3 space-y-1.5">
              {log.length === 0 && (
                <li className="font-mono text-[0.62rem] text-stale">watching the cluster…</li>
              )}
              {log.map((l, i) => (
                <li key={`${l.t}-${i}`} className="font-mono text-[0.68rem] leading-relaxed">
                  <span className="text-stale">{l.t}</span>{" "}
                  <span
                    className={
                      l.kind === "ok"
                        ? "text-converged"
                        : l.kind === "warn"
                          ? "text-drifting"
                          : "text-safelight"
                    }
                  >
                    {l.text}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        </aside>
      </div>
    </main>
  );
}
