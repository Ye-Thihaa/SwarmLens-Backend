/**
 * Server-side proxy for the console's chaos actions. Two things this
 * closes that the /console page gate alone couldn't:
 *
 * 1. "Route guards don't protect server functions" (see
 *    lib/consoleAuth.ts's own docstring) -- these RPC endpoints are
 *    reachable directly regardless of whether the caller ever loaded
 *    /console, so they re-check the session cookie themselves, on every
 *    call, not just once at page load.
 * 2. main.py's OPERATOR_TOKEN never touches the browser. If the chaos
 *    calls went straight from client JS to the FastAPI nodes (like the
 *    read-only ones in lib/api.ts still do), the token would have to
 *    live in client-side state to attach as a header -- exactly what
 *    HttpOnly cookies exist to avoid. Routing through here means only
 *    this Node process ever reads OPERATOR_TOKEN, straight from its own
 *    process.env (see server.ts's dotenv loader), and the browser only
 *    ever proves it has a valid console session, never the token itself.
 */

import { createServerFn } from "@tanstack/react-start";
import {
  CLUSTER,
  createHostedEvent,
  fetchJoinToken,
  healAllNodes,
  isolateNode,
  listHostedEvents,
  triggerRecap,
} from "./api";
import { checkConsoleSession } from "./consoleAuth";

async function requireConsoleSession(): Promise<void> {
  const { authorized } = await checkConsoleSession();
  if (!authorized) throw new Error("Not authorized");
}

function operatorHeaders(): Record<string, string> {
  const token = process.env["OPERATOR_TOKEN"];
  return token ? { "X-Operator-Token": token } : {};
}

export const serverIsolateNode = createServerFn({ method: "POST" })
  .validator((data: unknown) => {
    if (
      typeof data !== "object" ||
      data === null ||
      !("nodeId" in data) ||
      typeof (data as { nodeId: unknown }).nodeId !== "string"
    ) {
      throw new Error("Invalid request");
    }
    return { nodeId: (data as { nodeId: string }).nodeId };
  })
  .handler(async ({ data }) => {
    await requireConsoleSession();
    await isolateNode(data.nodeId, operatorHeaders());
    return { ok: true };
  });

export const serverHealAll = createServerFn({ method: "POST" }).handler(async () => {
  await requireConsoleSession();
  await healAllNodes(operatorHeaders());
  return { ok: true };
});

/** GET /events is gated the same way the chaos endpoints are (see
 * main.py's require_operator_token) -- a public directory of every event
 * on the cluster is exactly what multi-tenant hosting has to avoid.
 * Routed through here so OPERATOR_TOKEN stays server-side, same reasoning
 * as serverIsolateNode/serverHealAll above. Tries each cluster node in
 * turn: unlike the chaos actions (which target one specific node on
 * purpose), a directory listing just needs any node that's up. */
export const serverListEvents = createServerFn({ method: "GET" }).handler(async () => {
  await requireConsoleSession();
  const headers = operatorHeaders();
  for (const n of CLUSTER) {
    try {
      return await listHostedEvents(n.url, headers);
    } catch {
      continue;
    }
  }
  return { total: 0, limit: 50, offset: 0, events: [] };
});

/** Fetches one event's join token, on demand, for printing its QR. Kept
 * out of serverListEvents so the console's 5s directory poll doesn't
 * carry every event's credential -- see api.ts's HostedEventAdmin. */
export const serverEventToken = createServerFn({ method: "POST" })
  .validator((data: unknown) => {
    if (
      typeof data !== "object" ||
      data === null ||
      !("slug" in data) ||
      typeof (data as { slug: unknown }).slug !== "string"
    ) {
      throw new Error("Invalid request");
    }
    return { slug: (data as { slug: string }).slug };
  })
  .handler(async ({ data }) => {
    await requireConsoleSession();
    const headers = operatorHeaders();
    for (const n of CLUSTER) {
      try {
        return await fetchJoinToken(n.url, data.slug, headers);
      } catch {
        continue;
      }
    }
    return { event_id: "", slug: data.slug, join_token: "" };
  });

/** Calls the operator "end this event" action: freezes its recap
 * slideshow at whatever's most-liked right now. Broadcasts to every
 * cluster node like serverHealAll/serverIsolateNode -- the operator
 * doesn't (and shouldn't have to) know which node is the Raft leader, and
 * triggerRecap is a no-op everywhere except there. */
export const serverTriggerRecap = createServerFn({ method: "POST" })
  .validator((data: unknown) => {
    if (
      typeof data !== "object" ||
      data === null ||
      !("eventId" in data) ||
      typeof (data as { eventId: unknown }).eventId !== "string"
    ) {
      throw new Error("Invalid request");
    }
    return { eventId: (data as { eventId: string }).eventId };
  })
  .handler(async ({ data }) => {
    await requireConsoleSession();
    const headers = operatorHeaders();
    const results = await Promise.allSettled(
      CLUSTER.map((n) => triggerRecap(n.url, data.eventId, headers)),
    );
    const fired = results.some(
      (r) => r.status === "fulfilled" && r.value.triggered,
    );
    const reached = results.some((r) => r.status === "fulfilled");
    return { ok: reached, fired };
  });

export const serverCreateEvent = createServerFn({ method: "POST" })
  .validator((data: unknown) => {
    if (typeof data !== "object" || data === null) throw new Error("Invalid request");
    const d = data as Record<string, unknown>;
    if (typeof d["slug"] !== "string" || typeof d["name"] !== "string") {
      throw new Error("Invalid request");
    }
    return {
      slug: d["slug"] as string,
      name: d["name"] as string,
      venue: typeof d["venue"] === "string" ? d["venue"] : "",
      when: typeof d["when"] === "string" ? d["when"] : "",
      zones: Array.isArray(d["zones"]) ? (d["zones"] as string[]) : [],
      event_id: typeof d["event_id"] === "string" ? d["event_id"] : undefined,
    };
  })
  .handler(async ({ data }) => {
    await requireConsoleSession();
    // Any reachable node will do -- POST /events gossips out from
    // whichever one accepts it, same as a photo upload.
    for (const n of CLUSTER) {
      try {
        return await createHostedEvent(n.url, data, operatorHeaders());
      } catch {
        continue;
      }
    }
    return { ok: false, reason: "error" } as const;
  });
