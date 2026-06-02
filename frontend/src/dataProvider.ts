import { DataProvider, fetchUtils } from "react-admin";
import { env } from "./env";

// WA FastAPI 의 교육 입찰 지식 API. 레코드 1건 = 지식 문서(active 버전).
// id 규칙: "section" (공유) 또는 "section.track" (scoring_policy). 트랙은 점으로 구분.
const API = `${env.VITE_API_BASE}/api/edu-bid/knowledge`;

const httpClient = (url: string, options: fetchUtils.Options = {}) => {
  const headers = (options.headers as Headers) || new Headers({ Accept: "application/json" });
  const token = localStorage.getItem("token");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetchUtils.fetchJson(url, { ...options, headers });
};

const toId = (section: string, track: string) => (track ? `${section}.${track}` : section);

const parseId = (id: unknown): { section: string; track: string } => {
  const [section, track = ""] = String(id).split(".");
  return { section, track };
};

const q = (track: string) => `?track=${encodeURIComponent(track)}`;

export const dataProvider: DataProvider = {
  getList: async () => {
    const { json } = await httpClient(API);
    const data = json.map((d: any) => ({ id: toId(d.section, d.track), ...d }));
    return { data, total: data.length };
  },
  getOne: async (_resource, { id }) => {
    const { section, track } = parseId(id);
    const { json } = await httpClient(`${API}/${section}${q(track)}`);
    return {
      data: {
        id,
        section,
        track,
        payloadText: JSON.stringify(json.payload, null, 2),
        note: "",
      },
    } as any;
  },
  update: async (_resource, { id, data }) => {
    const { section, track } = parseId(id);
    let payload: unknown;
    try {
      payload = JSON.parse(data.payloadText);
    } catch (e) {
      throw new Error(`payload JSON 파싱 실패: ${(e as Error).message}`);
    }
    await httpClient(`${API}/${section}${q(track)}`, {
      method: "PUT",
      body: JSON.stringify({ payload, note: data.note ?? "" }),
    });
    return { data: { ...data, id } } as any;
  },
  // 단일 리소스(목록/조회/편집)만 쓰므로 나머지는 미지원.
  getMany: async () => ({ data: [] }),
  getManyReference: async () => ({ data: [], total: 0 }),
  create: async () => Promise.reject(new Error("미지원")),
  delete: async () => Promise.reject(new Error("미지원")),
  deleteMany: async () => Promise.reject(new Error("미지원")),
  updateMany: async () => Promise.reject(new Error("미지원")),
};
