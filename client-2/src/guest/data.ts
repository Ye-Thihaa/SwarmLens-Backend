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

/** The stock *choice* is local to each phone -- no filter name or ID is ever
 * sent to the server, and the live viewfinder strip is cosmetic only. But
 * routes/capture.tsx's post-capture review sheet bakes the picked stock's
 * look into real pixels (buildFilterCss -> canvas ctx.filter) before a
 * confirmed shot ever reaches the outbox, so a saved photo *does* carry the
 * look now -- just as ordinary image data, not as separate metadata.
 * `key` is a real contract with the backend either way: ai_engine.py's
 * FILM_STOCKS uses these exact keys, and /analyze/preview returns one of
 * them as suggested_filter so the strip can highlight what it recommends.
 * Rename a key on one side only and the recommendation silently stops
 * matching anything. `note` stays presentation, owned here. */
export const filmStocks = [
  { key: "portra_400", name: "Portra 400", note: "skin, candlelight" },
  { key: "cinestill_800t", name: "Cinestill 800T", note: "tungsten, halation" },
  { key: "tri_x_400", name: "Tri-X 400", note: "black & white, grain" },
  { key: "ektar_100", name: "Ektar 100", note: "daylight, saturated" },
  { key: "gold_200", name: "Gold 200", note: "warm, forgiving" },
];
