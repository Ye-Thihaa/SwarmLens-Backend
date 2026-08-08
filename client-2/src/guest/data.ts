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

/** Purely cosmetic, local to each phone -- this backend has no film-stock
 * concept (ai_engine.py's suggested_filter is a different, real ML
 * output; these are just camera flavor). Never sent to the server. */
export const filmStocks = [
  { name: "Portra 400", note: "skin, candlelight" },
  { name: "Cinestill 800T", note: "tungsten, halation" },
  { name: "Tri-X 400", note: "black & white, grain" },
  { name: "Ektar 100", note: "daylight, saturated" },
  { name: "Gold 200", note: "warm, forgiving" },
];
