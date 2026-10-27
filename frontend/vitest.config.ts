/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import path from "node:path";

// Kept separate from vite.config.ts on purpose: vitest ships its own nested copy of vite, so
// mixing its `defineConfig` with the build plugins makes the two vite type trees collide.
// Tests do not need the React or Tailwind plugins — esbuild reads `jsx` from tsconfig, and the
// component tests assert on text and roles, not on computed styles.
export default defineConfig({
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
