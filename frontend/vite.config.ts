import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// FastAPI(main.py)가 /admin 하위로 정적 서빙하므로 base 를 /admin/ 로 둔다.
export default defineConfig({
  base: "/admin/",
  plugins: [react()],
  server: {
    // 로컬 개발: WA FastAPI(8000)로 API 프록시
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
  },
});
