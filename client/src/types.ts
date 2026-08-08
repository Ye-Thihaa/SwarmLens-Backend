export interface VClock {
  [deviceId: string]: number;
}

/** One row in the offline outbox. Written immediately on capture (before
 * any network attempt), rendered optimistically, then synced in the
 * background -- see ROADMAP.md's Phase 5 spec. */
export interface OutboxPhoto {
  local_id: string; // uuid, primary key
  kind: "photo";
  guest_id: string;
  zone: string;
  composition_score: number;
  vclock: VClock;
  blob: Blob; // the captured image -- this backend has no photo-binary
  // storage of its own (see ai_engine.py's /analyze docstring), so the
  // local blob is the only place these bytes live once synced.
  created_at: number; // Date.now(), local display ordering only -- NOT
  // the causal order, which is what vclock is for.
  synced: 0 | 1; // Dexie index-friendly boolean
  photo_id?: string; // assigned by the backend once synced
  synced_at?: number;
  error?: string;
}

/** Shape returned by GET /photos on a node -- see main.py's list_photos. */
export interface RemotePhoto {
  photo_id: string;
  guest_id: string;
  zone: string;
  url: string;
  composition_score: number;
  likes: number;
  stored_on: string;
  vclock: VClock;
  concurrent_with: string[];
}
