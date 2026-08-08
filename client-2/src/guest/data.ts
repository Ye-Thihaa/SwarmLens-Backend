import dancefloor from "@/assets/frame-dancefloor.jpg";

/** Guest App flavor data. This backend (../../main.py) has no multi-event
 * or multi-tenancy concept -- just one continuously running 3-node
 * cluster -- so there is exactly one real "room" here, not a directory of
 * them. guests/frames/live counts are computed live from GET /photos and
 * GET /health (see src/lib/api.ts), never hardcoded. */

export type EventEntry = {
  slug: string;
  name: string;
  venue: string;
  when: string;
  hero: string;
};

export const events: EventEntry[] = [
  {
    slug: "hollis-marchetti",
    name: "Hollis × Marchetti",
    venue: "Cordwainers' Hall, Bermondsey",
    when: "Tonight",
    hero: dancefloor,
  },
];

/** The look itself is still local to each phone -- picking a stock sends
 * nothing to the server, and no photo carries one. But `key` is now a
 * real contract with the backend: ai_engine.py's FILM_STOCKS uses these
 * exact keys, and /analyze/preview returns one of them as
 * suggested_filter so the strip can highlight what it recommends. Rename
 * a key on one side only and the recommendation silently stops matching
 * anything. `note` stays presentation, owned here. */
export const filmStocks = [
  { key: "portra_400", name: "Portra 400", note: "skin, candlelight" },
  { key: "cinestill_800t", name: "Cinestill 800T", note: "tungsten, halation" },
  { key: "tri_x_400", name: "Tri-X 400", note: "black & white, grain" },
  { key: "ektar_100", name: "Ektar 100", note: "daylight, saturated" },
  { key: "gold_200", name: "Gold 200", note: "warm, forgiving" },
];
