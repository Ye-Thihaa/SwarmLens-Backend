/**
 * Offline-first local queue: photos and likes get written here
 * immediately, rendered optimistically, then synced to the cluster in
 * the background. Same shape as the Phase 5 reference client's Dexie
 * outbox (client/src/db.ts + sync.ts), backed by localStorage instead of
 * IndexedDB -- simpler for this app, at the cost of localStorage's
 * ~5-10MB per-origin ceiling, which is fine for a demo's worth of queued
 * photos but not a real deployment's.
 */

import { useSyncExternalStore } from "react";
import { pickNode, postAnalyze, postLike, postPhoto } from "./api";

export type OutboxPhoto = {
  local_id: string;
  kind: "photo";
  guest_id: string;
  zone: string;
  composition_score: number;
  vclock: Record<string, number>;
  image_base64: string;
  created_at: number;
  synced: boolean;
  photo_id?: string;
  error?: string;
};

export type OutboxLike = {
  local_id: string;
  kind: "like";
  guest_id: string;
  photo_id: string;
  vclock: Record<string, number>;
  created_at: number;
  synced: boolean;
  error?: string;
};

export type OutboxItem = OutboxPhoto | OutboxLike;

const STORAGE_KEY = "swarmlens_outbox";

function readAll(): OutboxItem[] {
  if (typeof window === "undefined") return [];
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]") as OutboxItem[];
  } catch {
    return [];
  }
}

function writeAll(items: OutboxItem[]) {
  if (typeof window === "undefined") return;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

const listeners = new Set<() => void>();
function notify() {
  for (const fn of listeners) fn();
}
function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

const EMPTY: OutboxItem[] = [];

// useSyncExternalStore requires a stable (===) snapshot when nothing
// changed -- readAll() re-parses JSON on every call, which is a *new*
// array reference every time even when localStorage is untouched, so
// React sees "changed" on every render and loops forever. Cache against
// the raw string and only re-parse when it actually differs.
let cachedRaw: string | null = null;
let cachedSnapshot: OutboxItem[] = EMPTY;

function getSnapshot(): OutboxItem[] {
  if (typeof window === "undefined") return EMPTY;
  const raw = localStorage.getItem(STORAGE_KEY) ?? "[]";
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    try {
      cachedSnapshot = JSON.parse(raw) as OutboxItem[];
    } catch {
      cachedSnapshot = EMPTY;
    }
  }
  return cachedSnapshot;
}

export function useOutbox(): OutboxItem[] {
  return useSyncExternalStore(subscribe, getSnapshot, () => EMPTY);
}

export function addPhoto(item: Omit<OutboxPhoto, "kind" | "synced" | "created_at">): OutboxPhoto {
  const full: OutboxPhoto = { ...item, kind: "photo", synced: false, created_at: Date.now() };
  writeAll([full, ...readAll()]);
  notify();
  return full;
}

export function addLike(item: Omit<OutboxLike, "kind" | "synced" | "created_at">): OutboxLike {
  const full: OutboxLike = { ...item, kind: "like", synced: false, created_at: Date.now() };
  writeAll([full, ...readAll()]);
  notify();
  return full;
}

function patch(local_id: string, changes: Partial<OutboxItem>) {
  writeAll(
    readAll().map((it) => (it.local_id === local_id ? ({ ...it, ...changes } as OutboxItem) : it)),
  );
  notify();
}

/** Drains every unsynced outbox row against whichever node answers
 * fastest. Never throws -- an unreachable cluster is a routine, expected
 * state here, not an error. Same shape as client/src/sync.ts's
 * syncOutbox in the Phase 5 reference client. */
export async function syncOutbox(): Promise<{ attempted: number; synced: number }> {
  const pending = readAll().filter((it) => !it.synced);
  let synced = 0;
  for (const item of pending) {
    if (await syncOne(item)) synced++;
  }
  return { attempted: pending.length, synced };
}

async function syncOne(item: OutboxItem): Promise<boolean> {
  const node = await pickNode();
  if (!node) return false; // stay queued -- no node reachable right now
  try {
    if (item.kind === "photo") {
      const res = await postPhoto(node, {
        guest_id: item.guest_id,
        zone: item.zone,
        composition_score: item.composition_score,
        vclock: item.vclock,
        image_base64: item.image_base64,
      });
      patch(item.local_id, { synced: true, photo_id: res.photo_id });
      // Fire-and-forget: populates a real aesthetic_score for this photo
      // (see api.ts's postAnalyze docstring). Never blocks the outbox --
      // a cold model download shouldn't stall every other queued item.
      void postAnalyze(node, { photo_id: res.photo_id, image_base64: item.image_base64 }).catch(
        () => {},
      );
    } else {
      await postLike(node, {
        guest_id: item.guest_id,
        photo_id: item.photo_id,
        vclock: item.vclock,
      });
      patch(item.local_id, { synced: true });
    }
    return true;
  } catch (e) {
    patch(item.local_id, { error: String(e) });
    return false;
  }
}

export function hasLiked(photoId: string): boolean {
  return readAll().some((it) => it.kind === "like" && it.photo_id === photoId);
}
