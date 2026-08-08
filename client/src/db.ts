import Dexie, { type Table } from "dexie";
import type { OutboxItem } from "./types";

class SwarmLensDB extends Dexie {
  outbox!: Table<OutboxItem, string>;

  constructor() {
    super("swarmlens-guest");
    // `synced` indexed so the sync engine can query "everything not yet
    // synced" directly instead of a full-table scan + filter.
    this.version(1).stores({
      outbox: "local_id, synced, created_at",
    });
  }
}

export const db = new SwarmLensDB();
