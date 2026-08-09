/**
 * Shared between routes/capture.tsx (live preview + shot-time bake-in) and
 * routes/album.tsx (a whole composited strip, layered over already-baked
 * photos the same way a real photo booth's single house filter sits over
 * everyone's prints). One formula, one place, rather than two copies
 * drifting apart the first time either gets tuned.
 */

/** Per-stock starting point -- picking a stock resets to its own preset
 * rather than keeping whatever the previous stock's sliders were left at,
 * matching a real film swap more than a shared global edit. Keyed by the
 * same filmStocks[].key contract ai_engine.py's FILM_STOCKS uses. */
export const STOCK_PRESETS: Record<string, { tone: number; color: number; palette: number }> = {
  portra_400: { tone: -10, color: 85, palette: 65 },
  cinestill_800t: { tone: -20, color: 100, palette: 80 },
  tri_x_400: { tone: 0, color: 0, palette: 100 },
  ektar_100: { tone: 10, color: 130, palette: 60 },
  gold_200: { tone: 5, color: 90, palette: 70 },
};
export const DEFAULT_PRESET = { tone: 0, color: 100, palette: 50 };

/** TONE/COLOR/PALETTE -> a real canvas `filter` string.
 *
 * TONE drives brightness/contrast around neutral; COLOR is a direct
 * saturation percentage (0 = grayscale, 100 = unchanged); PALETTE (0..100)
 * scales each stock's own characteristic secondary treatment, so turning
 * it down fades toward "no particular stock" rather than toward black. */
export function buildFilterCss(
  stockKey: string,
  tone: number,
  color: number,
  palette: number,
): string {
  const brightness = 1 + tone / 250;
  const contrast = 1 + Math.abs(tone) / 400;
  const saturate = color / 100;
  const p = palette / 100;

  const parts = [
    `brightness(${brightness.toFixed(3)})`,
    `contrast(${contrast.toFixed(3)})`,
    `saturate(${saturate.toFixed(3)})`,
  ];

  switch (stockKey) {
    case "tri_x_400":
      parts.push(`grayscale(${p.toFixed(3)})`, `contrast(${(1 + 0.25 * p).toFixed(3)})`);
      break;
    case "cinestill_800t":
      parts.push(`hue-rotate(${(-6 * p).toFixed(1)}deg)`, `saturate(${(1 + 0.3 * p).toFixed(3)})`);
      break;
    case "ektar_100":
      parts.push(`saturate(${(1 + 0.4 * p).toFixed(3)})`);
      break;
    case "gold_200":
      parts.push(`sepia(${(0.25 * p).toFixed(3)})`);
      break;
    case "portra_400":
      parts.push(`sepia(${(0.12 * p).toFixed(3)})`, `saturate(${(1 - 0.1 * p).toFixed(3)})`);
      break;
  }
  return parts.join(" ");
}
