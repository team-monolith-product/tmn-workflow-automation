import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { authProvider } from "./authProvider";

// Doorkeeper implicit 콜백 — URL fragment 의 access_token 으로 로그인 후 해시 정리.
async function bootstrap() {
  const hash = new URLSearchParams(window.location.hash.slice(1));
  const accessToken = hash.get("access_token");
  if (accessToken) {
    await authProvider.login({ access_token: accessToken });
    window.history.replaceState({}, "", window.location.pathname);
  }
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}

bootstrap();
