/**
 * Persistent per-browser-profile guest identity + vector clock. No login
 * anywhere in this system. Same pattern as the Phase 5 reference client's
 * client/src/guest.ts + client/src/vclock.ts -- device_id and guest_id
 * are both just stable labels a browser profile picks for itself once.
 * SSR-safe: every read/write is guarded so these are no-ops on the
 * server, since localStorage doesn't exist there. Only ever call these
 * from client handlers/effects, never from a route loader's render body.
 */

import { DEFAULT_EVENT_ID } from "./event";

const DEVICE_ID_KEY = "swarmlens_device_id";
const GUEST_ID_KEY = "swarmlens_guest_id";
const CLOCK_KEY = "swarmlens_vclock";

const ADJECTIVES = ["swift", "golden", "quiet", "bold", "lucky", "bright", "calm", "eager"];
const HANDLES = ["otter", "falcon", "fox", "heron", "lynx", "wren", "hare", "mole"];

function randomGuestName(): string {
  const a = ADJECTIVES[Math.floor(Math.random() * ADJECTIVES.length)];
  const n = HANDLES[Math.floor(Math.random() * HANDLES.length)];
  return `${a}-${n}-${Math.floor(Math.random() * 100)}`;
}

export function currentDeviceId(): string {
  if (typeof window === "undefined") return "ssr";
  let id = localStorage.getItem(DEVICE_ID_KEY);
  if (!id) {
    id = crypto.randomUUID().slice(0, 8);
    localStorage.setItem(DEVICE_ID_KEY, id);
  }
  return id;
}

/** Guest identity is per EVENT, not per browser: the same phone at a
 * wedding and at a donation ceremony is two separate guests, with two
 * separate rolls and two separate public-gallery quotas (main.py enforces
 * the cap per guest per event). Sharing one id would put both events'
 * frames in one "my roll" and spend one event's quota on the other's.
 *
 * The default event deliberately keeps the un-suffixed legacy key. Photos
 * already in the log are owned by whatever guest_id that key holds, and
 * main.py checks ownership against the photo's own record -- renaming the
 * key would quietly cost this browser the right to publish or delete
 * everything it had already shot. */
export function currentGuestId(eventId: string = DEFAULT_EVENT_ID): string {
  if (typeof window === "undefined") return "guest";
  const key = eventId === DEFAULT_EVENT_ID ? GUEST_ID_KEY : `${GUEST_ID_KEY}:${eventId}`;
  let id = localStorage.getItem(key);
  if (!id) {
    id = randomGuestName();
    localStorage.setItem(key, id);
  }
  return id;
}

function getClock(): Record<string, number> {
  if (typeof window === "undefined") return {};
  const raw = localStorage.getItem(CLOCK_KEY);
  return raw ? (JSON.parse(raw) as Record<string, number>) : {};
}

/** Increments this device's own counter and returns the new clock
 * snapshot to attach to the event about to be created. Call once per
 * locally-originated event (a photo capture or a like), never on receipt
 * of a remote one. */
export function tick(): Record<string, number> {
  if (typeof window === "undefined") return {};
  const deviceId = currentDeviceId();
  const clock = getClock();
  clock[deviceId] = (clock[deviceId] ?? 0) + 1;
  localStorage.setItem(CLOCK_KEY, JSON.stringify(clock));
  return { ...clock };
}
