import type { VClock } from "./types";

const DEVICE_ID_KEY = "swarmlens_device_id";
const CLOCK_KEY = "swarmlens_vclock";

// Per ROADMAP.md's Phase 5 spec: "this one's fine as browser storage --
// it's small and disposable." Losing it (cleared profile, private
// browsing) just means this device's next event looks like it has no
// causal history -- never wrong, just less informative. See main.py's
// _concurrent: an empty/unknown clock is never flagged concurrent with
// anything, not asserted as simultaneous.

function getDeviceId(): string {
  let id = localStorage.getItem(DEVICE_ID_KEY);
  if (!id) {
    id = crypto.randomUUID().slice(0, 8);
    localStorage.setItem(DEVICE_ID_KEY, id);
  }
  return id;
}

function getClock(): VClock {
  const raw = localStorage.getItem(CLOCK_KEY);
  return raw ? (JSON.parse(raw) as VClock) : {};
}

function setClock(clock: VClock) {
  localStorage.setItem(CLOCK_KEY, JSON.stringify(clock));
}

/** Increments this device's own counter and returns the new full clock
 * snapshot to attach to the event about to be created. Call once per
 * locally-originated event (never on receipt of a remote one -- this
 * client doesn't merge remote clocks in, it only ever advances its own
 * dimension, same as the backend's origin/seq counters). */
export function tick(): VClock {
  const deviceId = getDeviceId();
  const clock = getClock();
  clock[deviceId] = (clock[deviceId] ?? 0) + 1;
  setClock(clock);
  return { ...clock };
}

export function currentDeviceId(): string {
  return getDeviceId();
}
