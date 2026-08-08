/**
 * Persistent per-browser-profile guest identity + vector clock. No login
 * anywhere in this system. Same pattern as the Phase 5 reference client's
 * client/src/guest.ts + client/src/vclock.ts -- device_id and guest_id
 * are both just stable labels a browser profile picks for itself once.
 * SSR-safe: every read/write is guarded so these are no-ops on the
 * server, since localStorage doesn't exist there. Only ever call these
 * from client handlers/effects, never from a route loader's render body.
 */

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

export function currentGuestId(): string {
  if (typeof window === "undefined") return "guest";
  let id = localStorage.getItem(GUEST_ID_KEY);
  if (!id) {
    id = randomGuestName();
    localStorage.setItem(GUEST_ID_KEY, id);
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
