/**
 * Printable join-card QR codes for the operator console. `qrcode`'s
 * `toString(..., { type: "svg" })` path does its own matrix generation in
 * pure JS -- no <canvas>, so the same call works during SSR and produces a
 * crisp, infinitely-scalable print, unlike a canvas/PNG raster that goes
 * blocky at table-card size.
 */

import QRCode from "qrcode";

export async function joinQrSvg(url: string): Promise<string> {
  return QRCode.toString(url, { type: "svg", margin: 1, width: 320 });
}

/** Opens a printable table card in its own tab -- same "hand it to
 * whatever printer is available" spirit as routes/album.tsx's
 * printCanvas, and the same reason it must run synchronously inside the
 * click handler that calls it: popup blockers eat an async-opened tab. */
export function printJoinCard(svg: string, eventName: string, venue: string) {
  const win = window.open("", "_blank");
  if (!win) return;
  win.document.write(`<!doctype html><html><head><title>${eventName} — scan to join</title><style>
    @page { margin: 0; }
    body {
      margin: 0; min-height: 100vh; display: flex; flex-direction: column;
      align-items: center; justify-content: center; gap: 1.25rem;
      background: #fff; color: #111; font-family: system-ui, sans-serif; text-align: center;
    }
    svg { width: 60vmin; height: 60vmin; }
    h1 { font-size: 1.5rem; margin: 0; }
    p { margin: 0; font-size: 1rem; color: #555; }
  </style></head><body>
    ${svg}
    <h1>${eventName}</h1>
    ${venue ? `<p>${venue}</p>` : ""}
    <p>Scan to walk in and start shooting</p>
  </body></html>`);
  win.document.close();
  win.onload = () => win.print();
}
