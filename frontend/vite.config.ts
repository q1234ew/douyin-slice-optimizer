import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  base: "/static/dashboard/",
  plugins: [vue()],
  build: {
    outDir: "../src/dso/api/static/dashboard",
    emptyOutDir: true
  },
  server: {
    port: 5173,
    proxy: {
      "/stats": "http://127.0.0.1:8000",
      "/runtime": "http://127.0.0.1:8000",
      "/providers": "http://127.0.0.1:8000",
      "/videos": "http://127.0.0.1:8000",
      "/segments": "http://127.0.0.1:8000",
      "/variants": "http://127.0.0.1:8000",
      "/exports": "http://127.0.0.1:8000",
      "/metrics": "http://127.0.0.1:8000",
      "/platform": "http://127.0.0.1:8000",
      "/accounts": "http://127.0.0.1:8000",
      "/learning": "http://127.0.0.1:8000",
      "/training-samples": "http://127.0.0.1:8000",
      "/feedback": "http://127.0.0.1:8000"
    }
  }
});
