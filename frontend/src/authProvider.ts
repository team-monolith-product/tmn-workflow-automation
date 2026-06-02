import { AuthProvider } from "react-admin";
import { env } from "./env";

// admin-rails(Doorkeeper) 임시(implicit) 흐름 — admin-react authProvider 미러.
// 토큰 없이 로그인 시 admin-rails 로 리다이렉트 → 콜백 fragment 의 access_token 으로 로그인.
const authUri = `${env.VITE_AUTH_SERVER}/oauth/authorize`;
const redirectUri = `${window.location.origin}/admin/`;

export const authProvider: AuthProvider = {
  login: async (params: { access_token?: string } = {}) => {
    if (!params.access_token) {
      const q = new URLSearchParams();
      q.append("client_id", env.VITE_CLIENT_ID);
      q.append("redirect_uri", redirectUri);
      q.append("response_type", "token");
      q.append("scope", "public");
      window.location.href = `${authUri}?${q.toString()}`;
      return Promise.reject();
    }
    const resp = await fetch(`${env.VITE_AUTH_SERVER}/api/v1/me`, {
      headers: { Authorization: `Bearer ${params.access_token}` },
    });
    if (!resp.ok) return Promise.reject();
    const me = await resp.json();
    localStorage.setItem("token", params.access_token);
    localStorage.setItem("current_admin", JSON.stringify(me));
  },
  logout: async () => {
    localStorage.removeItem("token");
    localStorage.removeItem("current_admin");
  },
  checkError: async (error: { status?: number }) => {
    if (error?.status === 401 || error?.status === 403) {
      localStorage.clear();
      throw new Error();
    }
  },
  checkAuth: async () =>
    localStorage.getItem("token") ? undefined : Promise.reject(),
  getIdentity: async () => {
    const me = JSON.parse(localStorage.getItem("current_admin") || "{}");
    return { id: me.email || me.id || "me", fullName: me.name || me.email || "관리자" };
  },
  getPermissions: async () => {
    const me = JSON.parse(localStorage.getItem("current_admin") || "{}");
    return me.permissions ?? [];
  },
};
