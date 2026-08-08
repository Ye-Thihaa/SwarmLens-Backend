import { db } from "./db";
import { pickNode } from "./nodes";
import type { OutboxPhoto } from "./types";

/** Drains every unsynced outbox row, attempting one POST /photos per
 * item against whichever node answers fastest. Safe to call from
 * anywhere -- the app on an 'online' event, a foreground poll, or the
 * service worker's 'sync' event handler (see sw.ts) -- since it always
 * re-reads the outbox fresh and only ever advances items that actually
 * succeed. Never throws: a node being unreachable is an expected,
 * routine outcome here, not an error condition. */
export async function syncOutbox(): Promise<{ attempted: number; synced: number }> {
  const pending = await db.outbox.where("synced").equals(0).toArray();
  let synced = 0;
  for (const item of pending) {
    if (await syncOne(item)) synced++;
  }
  return { attempted: pending.length, synced };
}

async function syncOne(item: OutboxPhoto): Promise<boolean> {
  const node = await pickNode();
  if (!node) {
    return false; // stay queued -- no node reachable right now
  }
  try {
    const r = await fetch(`${node}/photos`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        guest_id: item.guest_id,
        zone: item.zone,
        composition_score: item.composition_score,
        vclock: item.vclock,
      }),
    });
    if (!r.ok) throw new Error(`${node} responded ${r.status}`);
    const body = (await r.json()) as { photo_id: string };
    await db.outbox.update(item.local_id, {
      synced: 1,
      photo_id: body.photo_id,
      synced_at: Date.now(),
      error: undefined,
    });
    return true;
  } catch (e) {
    await db.outbox.update(item.local_id, { error: String(e) });
    return false;
  }
}
