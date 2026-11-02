import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: "../web/caption-studio/assets",
    emptyOutDir: true,
    lib: {
      entry: "src/main.tsx",
      formats: ["es"],
      fileName: () => "caption-studio-app.js",
    },
    rollupOptions: {
      output: {
        inlineDynamicImports: true,
        assetFileNames: "caption-studio-app.[ext]",
      },
    },
    cssCodeSplit: false,
  },
});
