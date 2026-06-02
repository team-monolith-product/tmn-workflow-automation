/// <reference types="vite/client" />

// 빌드타임 VITE_* 환경변수. admin-rails(Doorkeeper)로 로그인하고 WA API 를 호출한다.
type EnvType = {
  VITE_AUTH_SERVER: string; // admin-rails 베이스 URL (Doorkeeper)
  VITE_CLIENT_ID: string; // WA 어드민용 Doorkeeper application client_id
  VITE_API_BASE: string; // WA FastAPI 베이스 (기본: 동일 오리진)
};

export const env: EnvType = {
  VITE_AUTH_SERVER: import.meta.env.VITE_AUTH_SERVER ?? "",
  VITE_CLIENT_ID: import.meta.env.VITE_CLIENT_ID ?? "",
  VITE_API_BASE: import.meta.env.VITE_API_BASE ?? "",
};
