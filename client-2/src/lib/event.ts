/**
 * Which hosted event this phone has walked into.
 *
 * The cluster hosts several at once (a wedding on Saturday, a donation
 * ceremony on Sunday, both on the same three nodes and the same append-only
 * log -- see main.py's "hosted events" section). A guest never chooses from
 * a list: they scan the QR on the table, /join/$slug resolves it once, and
 * the resulting event_id is stored here and attached to everything this
 * device does from then on.
 *
 * SSR-safe in the same way as guest.ts: every read/write is guarded, so
 * these are no-ops on the server where localStorage doesn't exist. Only
 * call them from client handlers/effects.
 */

import { useSyncExternalStore } from "react";
import { events as fallbackRooms } from "@/guest/data";
import { ZONES } from "@/lib/api";

export type JoinedEvent = {
  event_id: string;
  slug: string;
  name: string;
  venue: string;
  when: string;
  /** Which corners of the room a frame may be tagged with. One entry
   * means the capture screen shows no picker at all -- see routes/capture.tsx. */
  zones: string[];
};

/** Mirrors store.py's DEFAULT_EVENT_ID. Every photo logged before
 * multi-event hosting existed belongs to it, so a guest who opens the app
 * directly (no QR) lands exactly where the single-event demo always went,
 * seeing the same room and the same five zones -- not an empty one. */
export const DEFAULT_EVENT_ID = "default";

const room = fallbackRooms[0]!;

export const DEFAULT_EVENT: JoinedEvent = {
  event_id: DEFAULT_EVENT_ID,
  slug: room.slug,
  name: room.name,
  venue: room.venue,
  when: room.when,
  zones: ZONES,
};

const KEY = "swarmlens_event";

const listeners = new Set<() => void>();
function notify() {
  for (const fn of listeners) fn();
}
function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Same stable-snapshot discipline as lib/outbox.ts: useSyncExternalStore
// compares by reference, so re-parsing the JSON on every getSnapshot call
// returns a new object every time and re-renders forever. Cache against the
// raw string and only re-parse when it actually differs.
let cachedRaw: string | null = null;
let cachedEvent: JoinedEvent = DEFAULT_EVENT;

function getSnapshot(): JoinedEvent {
  if (typeof window === "undefined") return DEFAULT_EVENT;
  const raw = localStorage.getItem(KEY);
  if (!raw) return DEFAULT_EVENT;
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    try {
      const parsed = JSON.parse(raw) as Partial<JoinedEvent>;
      cachedEvent = {
        ...DEFAULT_EVENT,
        ...parsed,
        zones: parsed.zones?.length ? parsed.zones : ["main"],
      };
    } catch {
      cachedEvent = DEFAULT_EVENT;
    }
  }
  return cachedEvent;
}

export function currentEvent(): JoinedEvent {
  return getSnapshot();
}

export function currentEventId(): string {
  return getSnapshot().event_id;
}

export function useCurrentEvent(): JoinedEvent {
  return useSyncExternalStore(subscribe, getSnapshot, () => DEFAULT_EVENT);
}

/** Called once, by /join/$slug, after the cluster has resolved a scanned
 * QR. Switching events is deliberately destructive to nothing: the outbox
 * keeps every queued item, each tagged with the event it was shot at, so a
 * photo taken offline at the wedding still syncs into the wedding after the
 * guest has walked into the next room. */
export function joinEvent(event: JoinedEvent) {
  if (typeof window === "undefined") return;
  localStorage.setItem(KEY, JSON.stringify(event));
  notify();
}

export function leaveEvent() {
  if (typeof window === "undefined") return;
  localStorage.removeItem(KEY);
  cachedRaw = null;
  cachedEvent = DEFAULT_EVENT;
  notify();
}
