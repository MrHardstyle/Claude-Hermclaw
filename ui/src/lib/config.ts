/**
 * API base URL resolution.
 *
 * Order: `<meta name="hermclaw-api-base">` in index.html (editable after deployment without a rebuild)
 * → build-time `VITE_HERMCLAW_API_BASE` → same-origin `/api`.
 */
export const DEFAULT_API_BASE = "/api";

export function normalizeApiBase(raw: string | null | undefined): string {
  const value = (raw ?? "").trim();
  if (!value) return DEFAULT_API_BASE;
  return value.replace(/\/+$/, "") || DEFAULT_API_BASE;
}

export function resolveApiBase(meta: string | null | undefined, env: string | null | undefined): string {
  if (meta && meta.trim()) return normalizeApiBase(meta);
  return normalizeApiBase(env);
}

function readMeta(): string | null {
  if (typeof document === "undefined") return null;
  return document.querySelector<HTMLMetaElement>('meta[name="hermclaw-api-base"]')?.content ?? null;
}

const envBase: unknown = import.meta.env.VITE_HERMCLAW_API_BASE;

export const API_BASE = resolveApiBase(readMeta(), typeof envBase === "string" ? envBase : null);

/** Join the API base with an endpoint path (`/jobs`) and optional query parameters. */
export function apiUrl(path: string, query?: Record<string, string | number | boolean | null | undefined>, base = API_BASE): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) {
    if (v === null || v === undefined || v === "") continue;
    params.set(k, String(v));
  }
  const qs = params.toString();
  return `${base}${p}${qs ? `?${qs}` : ""}`;
}
