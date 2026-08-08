import { useEffect, useState } from "react";
import { useLiveQuery } from "dexie-react-hooks";
import { db } from "../db";
import { syncOutbox } from "../sync";

/** Persistent status pill: online/offline + how many photos are still
 * queued. This is the "queued -- will sync" indicator the project's
 * task list describes -- built fresh here since this scaffold isn't
 * wired to the existing (not-attached) guest UI that already has one. */
export function OutboxStatus() {
  const [online, setOnline] = useState(navigator.onLine);
  const [syncing, setSyncing] = useState(false);
  const queuedCount = useLiveQuery(() => db.outbox.where("synced").equals(0).count(), [], 0);

  useEffect(() => {
    const goOnline = () => setOnline(true);
    const goOffline = () => setOnline(false);
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", goOffline);
    return () => {
      window.removeEventListener("online", goOnline);
      window.removeEventListener("offline", goOffline);
    };
  }, []);

  // Cross-browser fallback retry loop -- see sw.ts for the Background
  // Sync API path, which only Chromium currently supports. This foreground
  // poll is what makes reconnect-retry work everywhere else, and is cheap
  // (syncOutbox no-ops instantly when the outbox is empty).
  useEffect(() => {
    const attempt = async () => {
      setSyncing(true);
      try {
        await syncOutbox();
      } finally {
        setSyncing(false);
      }
    };
    void attempt();
    window.addEventListener("online", attempt);
    const interval = setInterval(attempt, 10_000);
    return () => {
      window.removeEventListener("online", attempt);
      clearInterval(interval);
    };
  }, []);

  const label = !online
    ? `Offline${queuedCount ? ` — ${queuedCount} queued` : ""}`
    : syncing
      ? "Syncing…"
      : queuedCount
        ? `${queuedCount} queued — will sync`
        : "Synced";

  return (
    <div className={`outbox-status ${online ? "online" : "offline"}`}>
      <span className="dot" />
      {label}
    </div>
  );
}
