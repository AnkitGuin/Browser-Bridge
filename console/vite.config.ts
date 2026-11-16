import { defineConfig } from "vite";

export default defineConfig({
  build: { assetsDir: "console-assets" },
  server: {
    proxy: Object.fromEntries(
      ["/api", "/assets", "/support", "/crm", "/inventory", "/health"].map(
        (path) => [path, "http://127.0.0.1:8000"],
      ),
    ),
  },
});
