const DEFAULT_NODES = [
  "http://localhost:8001",
  "http://localhost:8002",
  "http://localhost:8003",
];

function configuredNodes(): string[] {
  const fromEnv = import.meta.env.VITE_NODE_URLS as string | undefined;
  if (fromEnv) {
    return fromEnv
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  return DEFAULT_NODES;
}

export const NODES = configuredNodes();

/** Stands in for real geo-routing, which this repo has no infrastructure
 * for: race a cheap health check against every configured node and use
 * whichever answers first. Returns null if every node is unreachable
 * (offline, or the whole cluster is down) -- callers must treat that as
 * "stay queued," never as an error to surface loudly, since going
 * offline is an expected, routine state for this app, not a failure. */
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
