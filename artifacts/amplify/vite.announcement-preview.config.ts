// Builds the announcement preview widget as a single JS + CSS bundle under
// static/announcement-preview/, which Flask serves directly.
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
  // public/ belongs to the main SPA, not this widget.
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
        // Stable filenames so the Flask template can reference them directly.
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
