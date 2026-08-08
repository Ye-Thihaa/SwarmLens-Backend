import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      // injectManifest (not the default generateSW) because sw.ts needs a
      // custom 'sync' event handler for Background Sync -- generateSW only
      // builds a precache/runtime-caching worker, no room for app-specific
      // event handlers like draining the outbox.
      strategies: "injectManifest",
      srcDir: "src",
      filename: "sw.ts",
      injectManifest: {
        // this app has no meaningful offline "precache" beyond its own
        // shell -- the interesting offline behavior is the outbox, not
        // asset caching.
        globPatterns: ["**/*.{js,css,html,svg,png,ico}"],
      },
      registerType: "autoUpdate",
      devOptions: {
        enabled: true, // run the real service worker in `vite dev`, not just prod builds
        type: "module",
      },
      manifest: {
        name: "SwarmLens Guest",
        short_name: "SwarmLens",
        description: "Offline-first photo capture for SwarmLens events",
        theme_color: "#111318",
        background_color: "#111318",
        display: "standalone",
        start_url: "/",
        icons: [
          { src: "/favicon.svg", sizes: "any", type: "image/svg+xml" },
        ],
      },
    }),
  ],
});
