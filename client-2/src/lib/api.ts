/**
 * Talks to the real SwarmLens backend (../../main.py) -- 3 gossiping
 * nodes, no shared database, no central authority. Same node-picking
 * pattern as client/src/nodes.ts: race a cheap health check, use
 * whichever node answers first, treat "nobody answered" as a routine
 * offline state, never an error to surface loudly.
 */

export type RemotePhoto = {
  photo_id: string;
  guest_id: string;
  zone: string;
  url: string;
  composition_score: number;
  likes: number;
  stored_on: string;
  vclock: Record<string, number>;
  concurrent_with: string[];
  taken_at: number; // unix seconds
};

export type ZoneScore = {
  zone: string;
  photos: number;
  likes: number;
  avg_aesthetic: number | null;
  owner: string;
  stale: boolean;
};

export type QuorumResult = {
  node: string;
  N: number;
  R: number;
  W: number;
  strongly_consistent: boolean;
  queried: string[];
  unreachable: string[];
  zones: ZoneScore[];
};

export type PeerState = { reachable: boolean | null; last_ok: number | null; fails: number };

export type NodeHealth = {
  node: string;
  events: number;
  digest: Record<string, number>;
  gossip: {
    rounds: number;
    events_pulled: number;
    events_pushed: number;
    peers: Record<string, PeerState>;
    partitioned: string[];
  };
  raft: {
    node: string;
    role: "follower" | "candidate" | "leader";
    term: number;
    leader_id: string | null;
    voted_for: string | null;
  };
};

/** This repo's standard local run (see README.md / docker-compose.yml):
 * node1:8001 <-> node2:8002 <-> node3:8003, all-to-all peers -- same
 * layout dashboard.html hardcodes. `peers` order matters: a node's
 * /chaos/partition/{peer_index} indexes into *that node's own* PEERS
 * env var, positionally, not by a global node id -- see CLAUDE.md's
 * chaos gotcha. 127.0.0.1 (not localhost) to match how the nodes were
 * actually launched (SELF_URL/PEERS in README.md), so gossip.partitioned
 * entries reported by /health line up with URL_TO_ID below. */
type ClusterNode = { id: string; url: string; peers: string[] };

export const CLUSTER: ClusterNode[] = [
  { id: "node1", url: "http://127.0.0.1:8001", peers: ["node2", "node3"] },
  { id: "node2", url: "http://127.0.0.1:8002", peers: ["node1", "node3"] },
  { id: "node3", url: "http://127.0.0.1:8003", peers: ["node1", "node2"] },
];

function configuredNodeUrls(): string[] {
  const fromEnv = import.meta.env["VITE_NODE_URLS"] as string | undefined;
  if (fromEnv)
    return fromEnv
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  return CLUSTER.map((n) => n.url);
}

export const NODES = configuredNodeUrls();
export const CLUSTER_SIZE = NODES.length;
export const URL_TO_ID: Record<string, string> = Object.fromEntries(
  CLUSTER.map((n) => [n.url, n.id]),
);

/** Real zones this cluster actually knows about -- see client/src/constants.ts,
 * the Phase 5 reference client's equivalent list. */
export const ZONES = ["flower_arch", "bar", "dance_floor", "photo_booth", "entrance"];

export function prettyZone(zone: string): string {
  return zone.replace(/_/g, " ");
}

export async function pickNode(timeoutMs = 1500): Promise<string | null> {
  const attempts = NODES.map(async (url) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const r = await fetch(`${url}/health`, { signal: controller.signal });
      if (!r.ok) throw new Error(`${url} unhealthy`);
      return url;
    } finally {
      clearTimeout(timer);
    }
  });
  try {
    return await Promise.any(attempts);
  } catch {
    return null; // AggregateError -- every node failed or timed out
  }
}

async function asJson<T>(r: Response, what: string): Promise<T> {
  if (!r.ok) throw new Error(`${what} -> ${r.status}`);
  return r.json() as Promise<T>;
}

export const getHealth = (node: string) =>
  fetch(`${node}/health`).then((r) => asJson<NodeHealth>(r, `${node}/health`));

export const getPhotos = (node: string) =>
  fetch(`${node}/photos`)
    .then((r) => asJson<{ node: string; photos: RemotePhoto[] }>(r, `${node}/photos`))
    .then((b) => b.photos);

export function photoImageUrl(node: string, photoId: string): string {
  return `${node}/photos/${photoId}/image`;
}

export const postPhoto = (
  node: string,
  body: {
    guest_id: string;
    zone: string;
    composition_score: number;
    vclock: Record<string, number>;
    image_base64: string;
  },
) =>
  fetch(`${node}/photos`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => asJson<{ ok: boolean; photo_id: string; seq: number }>(r, `${node}/photos POST`));

/** Fires ai_engine.py's pretrained analysis (saliency AR guide, CLIP
 * film-stock suggestion, CLIP+LAION aesthetic score) on a photo this
 * device already has the bytes for. The resulting aesthetic_score event
 * feeds GET /zones' avg_aesthetic the normal way -- see main.py's
 * /analyze docstring. Can be slow on a cold model download (~154MB,
 * first call only); callers should treat this as fire-and-forget, never
 * block a photo's upload on it. */
export const postAnalyze = (node: string, body: { photo_id: string; image_base64: string }) =>
  fetch(`${node}/analyze`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) =>
    asJson<{ ar_guide: unknown; suggested_filter: string; aesthetic_score: number }>(
      r,
      `${node}/analyze`,
    ),
  );

type Rect = { x: number; y: number; w: number; h: number };

/** Directions come in matched, opposite pairs -- read the field names.
 * move_subject_* is which way the subject should shift *within the frame*;
 * pan_camera_* is which way to turn the camera to achieve that, which is
 * the inverse. Rendering the wrong one gives the guest a confidently
 * backwards arrow, so the UI should use pan_camera_* for anything shaped
 * like an instruction. See ai_engine.compute_ar_guide. */
export type ArGuide = {
  centroid: { x: number; y: number };
  image_size: { w: number; h: number };
  target: { x: number; y: number };
  dx: number;
  dy: number;
  move_subject_x: "left" | "right" | "centered";
  move_subject_y: "up" | "down" | "centered";
  pan_camera_x: "left" | "right" | "centered";
  pan_camera_y: "up" | "down" | "centered";
  strength: "none" | "slight" | "moderate" | "large";
  subject_found: boolean;
  well_composed: boolean;
};

/** rect/subject_box are normalized 0..1 against the frame that was sent,
 * so they overlay the viewfinder directly -- which is why the client must
 * send the frame cropped the way it's displayed (see grabPreviewFrame in
 * routes/capture.tsx), not the raw uncropped sensor image.
 *
 * `basis` distinguishes a real detected subject from ai_engine.py's
 * aesthetic-score fallback (used when no single subject was found but a
 * tighter crop still measurably scores better) -- the UI should not call
 * the latter a "subject" or imply anything was detected, just that a crop
 * measurably helped. `null` means neither found anything worth suggesting. */
export type Reframe = {
  rect: Rect;
  subject_box: Rect;
  zoom: number;
  worth_it: boolean;
  subject_found: boolean;
  basis: "subject" | "aesthetic" | null;
  aesthetic_gain?: number;
};

export type ComposePreview = {
  ar_guide: ArGuide;
  reframe: Reframe;
  scene: {
    colors: string[];
    saturation: "low" | "medium" | "high";
    brightness: "dark" | "even" | "bright";
    mean_saturation: number;
    mean_brightness: number;
  };
  suggested_filter: string;
  /** False when the top two stocks are within CONFIDENT_MARGIN of each
   * other -- i.e. a tie. The UI must soften its copy rather than assert a
   * preference the scores don't support. */
  confident: boolean;
  margin: number;
  picks: { key: string; name: string; score: number }[];
  reason: string;
  aesthetic_score: number;
};

/** Live viewfinder guidance, computed but never persisted (unlike
 * postAnalyze above, which appends an aesthetic_score event). Returns
 * null when the node sheds the request with 503 -- every preview slot
 * busy is an expected, routine outcome under load, not an error worth
 * surfacing: the caller just tries again on its next tick. */
export async function postAnalyzePreview(
  node: string,
  body: { image_base64: string; aspect: number },
  signal?: AbortSignal,
): Promise<ComposePreview | null> {
  const r = await fetch(`${node}/analyze/preview`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal: signal ?? null, // exactOptionalPropertyTypes: RequestInit wants null, not undefined
  });
  if (r.status === 503) return null;
  return asJson<ComposePreview>(r, `${node}/analyze/preview`);
}

export const postLike = (
  node: string,
  body: { guest_id: string; photo_id: string; vclock: Record<string, number> },
) =>
  fetch(`${node}/likes`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => asJson<{ ok: boolean; likes: number }>(r, `${node}/likes POST`));

export const getZones = (node: string) =>
  fetch(`${node}/zones`)
    .then((r) => asJson<{ node: string; zones: ZoneScore[] }>(r, `${node}/zones`))
    .then((b) => b.zones);

export const getZonesQuorum = (node: string, R: number, W: number) =>
  fetch(`${node}/zones/quorum?R=${R}&W=${W}`).then((r) =>
    asJson<QuorumResult>(r, `${node}/zones/quorum`),
  );

/** headers is how main.py's OPERATOR_TOKEN gate gets satisfied -- see
 * lib/operatorGateway.ts, the only real caller. A plain browser call
 * with no headers still works when a node has no OPERATOR_TOKEN
 * configured (main.py fails open when it's unset), which is why these
 * two stay exported and usable on their own, not folded into the
 * gateway file. */
export const chaosPartition = (
  node: string,
  peerIndex: number,
  headers: Record<string, string> = {},
) =>
  fetch(`${node}/chaos/partition/${peerIndex}`, { method: "POST", headers }).then((r) =>
    asJson<{ partitioned_from: string }>(r, `${node}/chaos/partition`),
  );

export const chaosHeal = (node: string, headers: Record<string, string> = {}) =>
  fetch(`${node}/chaos/heal`, { method: "POST", headers }).then((r) =>
    asJson<{ partitioned_from: string[] }>(r, `${node}/chaos/heal`),
  );

/** Fully isolate a node from the rest of the cluster: partition it in
 * BOTH directions from every other node, not just its own outgoing
 * gossip -- a one-sided partition self-heals within a gossip round or
 * two (CLAUDE.md's chaos gotcha). Same 4-call pattern as dashboard.html's
 * isolate(). Raft heartbeats are unaffected by this (they bypass
 * gossip.partitioned entirely -- see raft.py) so this demonstrates
 * gossip/quorum staleness, not a forced re-election. */
export async function isolateNode(
  nodeId: string,
  headers: Record<string, string> = {},
): Promise<void> {
  const node = CLUSTER.find((n) => n.id === nodeId);
  if (!node) return;
  const calls: Promise<unknown>[] = [];
  node.peers.forEach((peerId, idx) => {
    calls.push(chaosPartition(node.url, idx, headers));
    const peer = CLUSTER.find((n) => n.id === peerId)!;
    const backIdx = peer.peers.indexOf(node.id);
    calls.push(chaosPartition(peer.url, backIdx, headers));
  });
  await Promise.allSettled(calls);
}

export async function healAllNodes(headers: Record<string, string> = {}): Promise<void> {
  await Promise.allSettled(NODES.map((url) => chaosHeal(url, headers)));
}

/** How many of the N configured nodes already hold each photo_id, right
 * now -- a real replica count, not a simulated one. Tolerant of down/
 * partitioned nodes (allSettled): those simply don't contribute a count,
 * which is itself the point -- a low count during a partition is real
 * signal, not a bug to paper over. */
export async function replicaAcks(photoIds: string[]): Promise<Record<string, number>> {
  const counts: Record<string, number> = Object.fromEntries(photoIds.map((id) => [id, 0]));
  const results = await Promise.allSettled(NODES.map((n) => getPhotos(n)));
  for (const res of results) {
    if (res.status !== "fulfilled") continue;
    const ids = new Set(res.value.map((p) => p.photo_id));
    for (const id of photoIds) if (ids.has(id)) counts[id] = (counts[id] ?? 0) + 1;
  }
  return counts;
}
