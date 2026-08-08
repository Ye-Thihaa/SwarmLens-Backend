import { useEffect, useMemo, useState } from "react";
import { useLiveQuery } from "dexie-react-hooks";
import { db } from "../db";
import { pickNode } from "../nodes";
import { syncOutbox } from "../sync";
import { tick } from "../vclock";
import { currentGuestId } from "../guest";
import type { OutboxItem, OutboxPhoto, RemotePhoto } from "../types";

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
    [] as OutboxItem[],
  );

  // photo_id -> local blob, for the subset of remote entries that are
  // actually this device's own already-synced captures. This backend
  // has no photo-binary storage of its own (see ai_engine.py's /analyze
  // docstring) -- the local blob is the only real image data available
  // for anything, so remote entries without a local match render as a
  // metadata-only card, honestly, instead of a broken image.
  const ownBlobs = useMemo(() => {
    const map = new Map<string, Blob>();
    for (const o of outbox) if (o.kind === "photo" && o.photo_id) map.set(o.photo_id, o.blob);
    return map;
  }, [outbox]);

  // photo_ids this device has already tapped like on (queued or synced) --
  // used to show the button as already-liked instead of letting it be
  // tapped repeatedly. Not required for correctness (store.like_count is
  // a set-union CRDT over guest_id, so a duplicate like is a no-op
  // server-side), just to avoid firing pointless requests.
  const likedPhotoIds = useMemo(() => {
    const set = new Set<string>();
    for (const o of outbox) if (o.kind === "like") set.add(o.photo_id);
    return set;
  }, [outbox]);

  async function likePhoto(photoId: string) {
    await db.outbox.add({
      local_id: crypto.randomUUID(),
      kind: "like",
      guest_id: currentGuestId(),
      photo_id: photoId,
      vclock: tick(),
      created_at: Date.now(),
      synced: 0,
    });
    void syncOutbox();
  }

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

  const queued = outbox.filter((o): o is OutboxPhoto => o.kind === "photo" && !o.synced);
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
                <RemoteCard
                  key={p.photo_id}
                  photo={p}
                  blob={ownBlobs.get(p.photo_id)}
                  node={node}
                  liked={likedPhotoIds.has(p.photo_id)}
                  onLike={() => likePhoto(p.photo_id)}
                />
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

function RemoteCard({
  photo,
  blob,
  node,
  liked,
  onLike,
}: {
  photo: RemotePhoto;
  blob?: Blob;
  node: string | null;
  liked: boolean;
  onLike: () => void;
}) {
  const localUrl = useMemo(() => (blob ? URL.createObjectURL(blob) : null), [blob]);
  useEffect(() => () => { if (localUrl) URL.revokeObjectURL(localUrl); }, [localUrl]);

  // No local blob (this device didn't capture it) -- fall back to
  // fetching the image straight from a node, which can serve it because
  // gossip already replicated the full photo event (bytes included) to
  // every node. If that 404s (an older capture with no image_base64
  // attached), fall back to the honest metadata-only placeholder instead
  // of a broken image icon.
  const [remoteFailed, setRemoteFailed] = useState(false);
  const src = localUrl ?? (node && !remoteFailed ? `${node}/photos/${photo.photo_id}/image` : null);

  return (
    <div className="photo-card">
      {src ? (
        <img src={src} alt={photo.zone} onError={() => setRemoteFailed(true)} />
      ) : (
        <div className="photo-placeholder">{photo.zone.replace("_", " ")}</div>
      )}
      <div className="photo-meta">
        <span>{photo.guest_id}</span>
        <button
          className={`like-button ${liked ? "liked" : ""}`}
          onClick={onLike}
          disabled={liked}
          aria-label={liked ? "Liked" : "Like this photo"}
        >
          ♥ {photo.likes}
        </button>
      </div>
    </div>
  );
}
