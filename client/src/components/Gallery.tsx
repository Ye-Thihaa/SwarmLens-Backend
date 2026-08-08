import { useEffect, useMemo, useState } from "react";
import { useLiveQuery } from "dexie-react-hooks";
import { db } from "../db";
import { pickNode } from "../nodes";
import type { OutboxPhoto, RemotePhoto } from "../types";

/** Groups photos into connected components by `concurrent_with` (an
 * undirected adjacency -- if A lists B, B lists A too, per main.py's
 * _concurrent, but this doesn't assume that and unions both directions
 * defensively). Each component renders as one cluster instead of a
 * false total order. */
function groupConcurrent(photos: RemotePhoto[]): RemotePhoto[][] {
  const byId = new Map(photos.map((p) => [p.photo_id, p]));
  const seen = new Set<string>();
  const groups: RemotePhoto[][] = [];

  for (const p of photos) {
    if (seen.has(p.photo_id)) continue;
    const group: RemotePhoto[] = [];
    const queue = [p.photo_id];
    while (queue.length) {
      const id = queue.pop()!;
      if (seen.has(id)) continue;
      seen.add(id);
      const photo = byId.get(id);
      if (!photo) continue;
      group.push(photo);
      for (const other of photo.concurrent_with) {
        if (!seen.has(other)) queue.push(other);
      }
    }
    groups.push(group);
  }
  return groups;
}

export function Gallery() {
  const [remote, setRemote] = useState<RemotePhoto[]>([]);
  const [node, setNode] = useState<string | null>(null);

  const outbox = useLiveQuery(
    () => db.outbox.orderBy("created_at").reverse().toArray(),
    [],
    [] as OutboxPhoto[],
  );

  // photo_id -> local blob, for the subset of remote entries that are
  // actually this device's own already-synced captures. This backend
  // has no photo-binary storage of its own (see ai_engine.py's /analyze
  // docstring) -- the local blob is the only real image data available
  // for anything, so remote entries without a local match render as a
  // metadata-only card, honestly, instead of a broken image.
  const ownBlobs = useMemo(() => {
    const map = new Map<string, Blob>();
    for (const o of outbox) if (o.photo_id) map.set(o.photo_id, o.blob);
    return map;
  }, [outbox]);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      const n = await pickNode();
      if (cancelled) return;
      setNode(n);
      if (!n) return;
      try {
        const r = await fetch(`${n}/photos`);
        const body = (await r.json()) as { photos: RemotePhoto[] };
        if (!cancelled) setRemote(body.photos);
      } catch {
        // stay on the last known-good list -- a blip shouldn't blank the gallery
      }
    }
    void poll();
    const interval = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const queued = outbox.filter((o) => !o.synced);
  const groups = groupConcurrent(remote);

  return (
    <div className="gallery-screen">
      {queued.length > 0 && (
        <section>
          <h2>Your queue</h2>
          <div className="photo-row">
            {queued.map((o) => (
              <QueuedCard key={o.local_id} item={o} />
            ))}
          </div>
        </section>
      )}

      <section>
        <h2>
          Gallery {node ? <span className="node-tag">via {node.replace("http://", "")}</span> : <span className="node-tag offline">no node reachable</span>}
        </h2>
        {groups.length === 0 && <p className="empty">No photos yet.</p>}
        {groups.map((group, i) => (
          <div key={i} className={group.length > 1 ? "concurrent-cluster" : "photo-row"}>
            {group.length > 1 && (
              <div className="concurrent-badge">
                {group.length} photos taken around the same time — order not implied
              </div>
            )}
            <div className="photo-row">
              {group.map((p) => (
                <RemoteCard key={p.photo_id} photo={p} blob={ownBlobs.get(p.photo_id)} />
              ))}
            </div>
          </div>
        ))}
      </section>
    </div>
  );
}

function QueuedCard({ item }: { item: OutboxPhoto }) {
  const url = useMemo(() => URL.createObjectURL(item.blob), [item.blob]);
  useEffect(() => () => URL.revokeObjectURL(url), [url]);
  return (
    <div className="photo-card queued">
      <img src={url} alt={item.zone} />
      <div className="photo-meta">
        <span>{item.zone.replace("_", " ")}</span>
        <span className={`badge ${item.error ? "error" : "pending"}`}>
          {item.error ? "retry pending" : "queued"}
        </span>
      </div>
    </div>
  );
}

function RemoteCard({ photo, blob }: { photo: RemotePhoto; blob?: Blob }) {
  const url = useMemo(() => (blob ? URL.createObjectURL(blob) : null), [blob]);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  return (
    <div className="photo-card">
      {url ? (
        <img src={url} alt={photo.zone} />
      ) : (
        <div className="photo-placeholder">{photo.zone.replace("_", " ")}</div>
      )}
      <div className="photo-meta">
        <span>{photo.guest_id}</span>
        <span>♥ {photo.likes}</span>
      </div>
    </div>
  );
}
