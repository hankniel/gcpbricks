import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` proxies the API to a local `uvicorn gcpbricks.console.app:app`.
export default defineConfig({
  plugins: [react({ jsxImportSource: "@emotion/react", babel: { plugins: [] } })],
  server: { proxy: { "/api": "http://localhost:8080" } },
});
