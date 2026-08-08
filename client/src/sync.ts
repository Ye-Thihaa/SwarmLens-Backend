import { db } from "./db";
import { pickNode } from "./nodes";
import type { OutboxItem } from "./types";

/** Drains every unsynced outbox row (photos and likes alike), attempting
 * one POST per item against whichever node answers fastest. Safe to call
 * from anywhere -- the app on an 'online' event, a foreground poll, or
 * the service worker's 'sync' event handler (see sw.ts) -- since it
 * always re-reads the outbox fresh and only ever advances items that
 * actually succeed. Never throws: a node being unreachable is an
 * expected, routine outcome here, not an error condition. */
export async function syncOutbox(): Promise<{ attempted: number; synced: number }> {
  const pending = await db.outbox.where("synced").equals(0).toArray();
  let synced = 0;
  for (const item of pending) {
    if (await syncOne(item)) synced++;
  }
  return { attempted: pending.length, synced };
}

/** This backend has no photo-binary storage of its own (see
 * ai_engine.py's /analyze docstring) -- the only way another guest's
 * device can ever see this capture is if the bytes ride along inside the
 * photo event itself, the same way everything else in this system
 * replicates. Chunked to avoid a call-stack blowup from spreading a large
 * Uint8Array into String.fromCharCode at once. */
async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const chunkSize = 8192;
  let binary = "";
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

async function syncOne(item: OutboxItem): Promise<boolean> {
  const node = await pickNode();
  if (!node) {
    return false; // stay queued -- no node reachable right now
  }
  try {
    if (item.kind === "photo") {
      const r = await fetch(`${node}/photos`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          guest_id: item.guest_id,
          zone: item.zone,
          composition_score: item.composition_score,
          vclock: item.vclock,
          image_base64: await blobToBase64(item.blob),
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
    } else {
      const r = await fetch(`${node}/likes`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          guest_id: item.guest_id,
          photo_id: item.photo_id,
          vclock: item.vclock,
        }),
      });
      if (!r.ok) throw new Error(`${node} responded ${r.status}`);
      await db.outbox.update(item.local_id, {
        synced: 1,
        synced_at: Date.now(),
        error: undefined,
      });
    }
    return true;
  } catch (e) {
    await db.outbox.update(item.local_id, { error: String(e) });
    return false;
  }
}
