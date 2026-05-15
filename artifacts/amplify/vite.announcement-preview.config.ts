// Standalone Vite config that builds the announcement preview React widget as a
// single ES module + CSS file under static/announcement-preview/. The Flask app
// already serves everything under static/ (see Flask(__init__) in app.py), and the
// templates/announcements.html template loads the output via <script type="module">
// + <link rel="stylesheet">.
import path from "path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "src"),
    },
    dedupe: ["react", "react-dom"],
  },
  root: path.resolve(import.meta.dirname),
  // Don't copy public/ into the widget output — those assets (favicon, opengraph)
  // belong to the main React SPA, not the announcement preview bundle.
  publicDir: false,
  build: {
    outDir: path.resolve(
      import.meta.dirname,
      "static",
      "announcement-preview",
    ),
    emptyOutDir: true,
    cssCodeSplit: false,
    sourcemap: false,
    rollupOptions: {
      input: path.resolve(
        import.meta.dirname,
        "src",
        "announcement-preview",
        "main.tsx",
      ),
      output: {
        // Predictable filenames so the Flask template can reference them directly
        // without a manifest lookup. There is exactly one entry, one chunk, and one
        // CSS file in this build.
        entryFileNames: "main.js",
        chunkFileNames: "[name].js",
        assetFileNames: (info) => {
          if (info.name && info.name.endsWith(".css")) return "main.css";
          return "assets/[name][extname]";
        },
      },
    },
  },
});
