// @lovable.dev/vite-tanstack-config already includes the following — do NOT add them manually
// or the app will break with duplicate plugins:
//   - TanStack devtools (dev-only, first), tanstackStart, viteReact, tailwindcss, tsConfigPaths,
//     nitro (build-only using cloudflare as a default target), VITE_* env injection, @ path alias,
//     React/TanStack dedupe, error logger plugins, and sandbox detection (port/host/strictPort).
// You can pass additional config via defineConfig({ vite: { ... }, etc... }) if needed.
import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "@lovable.dev/vite-tanstack-config";

// Dev-only HTTPS. getUserMedia (the camera in src/routes/capture.tsx) requires
// a secure context, and only `localhost` is exempt from that -- so over plain
// HTTP a phone at http://192.168.x.x:8080 loads the app fine and then silently
// has no camera at all. Generate the pair with:
//
//   cd client-2 && mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes \
//     -keyout certs/dev-key.pem -out certs/dev-cert.pem -days 365 \
//     -subj "/CN=swarmlens-dev" \
//     -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:<your-lan-ip>"
//
// The IPs must be in subjectAltName, not just the CN -- browsers have ignored
// CN for host matching for years. certs/ is gitignored; this is a local dev
// cert, never a shipped one. Missing files fall back to HTTP rather than
// failing to boot, so a fresh clone still runs without generating anything.
const certDir = path.resolve(import.meta.dirname, "certs");
const keyPath = path.join(certDir, "dev-key.pem");
const certPath = path.join(certDir, "dev-cert.pem");
const https =
  fs.existsSync(keyPath) && fs.existsSync(certPath)
    ? { key: fs.readFileSync(keyPath), cert: fs.readFileSync(certPath) }
    : undefined;

// Same-origin proxy to the three backend nodes. Two problems this solves at
// once, both of which only appear once you leave localhost:
//
//   1. Mixed content -- an HTTPS page may not fetch http:// URLs, so the
//      HTTPS dev server above would otherwise break every backend call.
//   2. `127.0.0.1` means *the phone* when the page is loaded on a phone.
//
// Proxying keeps the browser talking to one origin it already trusts and lets
// this dev server reach the nodes over loopback on the guest's behalf. Point
// the app at these paths with VITE_NODE_URLS=/n1,/n2,/n3 (see .env.example).
// The operator console is unaffected either way: it reads api.ts's CLUSTER
// (absolute 127.0.0.1 URLs) directly, not NODES, because /chaos/partition
// indexes positionally into a specific node's own PEERS list.
const nodeProxy = Object.fromEntries(
  [1, 2, 3].map((i) => [
    `/n${i}`,
    { target: `http://127.0.0.1:${8000 + i}`, changeOrigin: true, rewrite: (p: string) => p.replace(`/n${i}`, "") },
  ]),
);

export default defineConfig({
  tanstackStart: {
    // Redirect TanStack Start's bundled server entry to src/server.ts (our SSR error wrapper).
    // nitro/vite builds from this
    server: { entry: "server" },
  },
  vite: {
    server: {
      ...(https ? { https } : {}),
      proxy: nodeProxy,
    },
  },
});
