/// <reference lib="webworker" />
import { precacheAndRoute } from "workbox-precaching";
import { syncOutbox } from "./sync";
import { SYNC_TAG } from "./backgroundSync";

declare let self: ServiceWorkerGlobalScope;

// vite-plugin-pwa (injectManifest strategy) replaces this with the real
// precache list at build time -- required boilerplate for that strategy.
precacheAndRoute(self.__WB_MANIFEST);

self.skipWaiting();
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// The actual Background Sync handler: only fires on browsers that
// support it (Chromium), and only after registerBackgroundSync() in
// backgroundSync.ts has requested it. IndexedDB (and therefore Dexie,
// and therefore the outbox) is fully accessible from a service worker,
// so this reuses the exact same syncOutbox() the foreground app calls --
// no separate/duplicated sync logic to keep in sync with itself.
self.addEventListener("sync", (event) => {
  const syncEvent = event as unknown as { tag: string; waitUntil: (p: Promise<unknown>) => void };
  if (syncEvent.tag === SYNC_TAG) {
    syncEvent.waitUntil(syncOutbox());
  }
});
