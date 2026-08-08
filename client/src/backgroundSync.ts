const SYNC_TAG = "sync-outbox";

/** Registers a Background Sync request so a supporting browser can wake
 * the service worker to retry the outbox even after the tab is closed --
 * see sw.ts for the handler. This is a progressive enhancement, not the
 * primary retry path: Background Sync API is Chromium-only (no Safari,
 * no Firefox as of this writing). OutboxStatus.tsx's foreground
 * online-listener + periodic poll is what makes reconnect-retry actually
 * work everywhere; this just adds "retries even if you close the tab"
 * on browsers that support it. */
export async function registerBackgroundSync(): Promise<void> {
  if (!("serviceWorker" in navigator)) return;
  const registration = await navigator.serviceWorker.ready;
  if (!("sync" in registration)) return; // Background Sync unsupported here
  try {
    await (registration as ServiceWorkerRegistration & { sync: { register(tag: string): Promise<void> } }).sync.register(SYNC_TAG);
  } catch {
    // registration can fail (e.g. permission, or called too early) --
    // the foreground fallback in OutboxStatus.tsx covers this regardless
  }
}

export { SYNC_TAG };
