import { apiUrl } from "../lib/config";
import { getToken, notifyUnauthorized } from "../lib/auth";
import type {
  Artifact,
  BugRecord,
  ControlAction,
  ControlResult,
  DiffResponse,
  HermEvent,
  Job,
  JobCreateBody,
  JobDetail,
  JobList,
  ModelsResponse,
  PlanVersion,
  Repository,
  ResearchRun,
  ResourcesResponse,
  StatsResponse,
  StepDetail,
  VersionResponse,
  Worker,
  WorkerDetail,
} from "./types";
import { normalizeEvent } from "../lib/events";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

interface RequestOptions {
  method?: "GET" | "POST";
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  /** token override (login check before the token is stored) */
  token?: string;
}

/** Extract a readable message from FastAPI / Hermclaw error bodies. */
export function errorFromBody(status: number, body: unknown): ApiError {
  if (body && typeof body === "object") {
    const b = body as Record<string, unknown>;
    const err = b.error;
    if (err && typeof err === "object") {
      const e = err as Record<string, unknown>;
      return new ApiError(status, String(e.code ?? "ERROR"), String(e.message ?? `HTTP ${status}`), e.details);
    }
    const detail = b.detail;
    if (Array.isArray(detail)) {
      const msg = detail
        .map((d: unknown) => {
          if (d && typeof d === "object") {
            const dd = d as Record<string, unknown>;
            const loc = Array.isArray(dd.loc) ? dd.loc.filter((x) => x !== "body").join(".") : "";
            return `${loc ? `${loc}: ` : ""}${String(dd.msg ?? "")}`;
          }
          return String(d);
        })
        .join("; ");
      return new ApiError(status, "VALIDATION_FAILED", msg || `HTTP ${status}`, detail);
    }
    if (typeof detail === "string") return new ApiError(status, `HTTP_${status}`, detail);
  }
  return new ApiError(status, `HTTP_${status}`, `HTTP ${status}`);
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const token = opts.token ?? getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(apiUrl(path, opts.query), { method: opts.method ?? "GET", headers, body, signal: opts.signal });
  } catch (exc) {
    if (exc instanceof DOMException && exc.name === "AbortError") throw exc;
    throw new ApiError(0, "NETWORK", "Server nicht erreichbar");
  }
  if (!res.ok) {
    let parsed: unknown = null;
    try {
      parsed = await res.json();
    } catch {
      /* non-JSON error body */
    }
    const err = errorFromBody(res.status, parsed);
    if (res.status === 401 && opts.token === undefined) notifyUnauthorized();
    throw err;
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Authenticated binary download (artifact files need the bearer header, so plain links cannot be used). */
export async function fetchBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const token = getToken();
  const res = await fetch(apiUrl(path), { headers: token ? { Authorization: `Bearer ${token}` } : {}, signal });
  if (!res.ok) {
    let parsed: unknown = null;
    try {
      parsed = await res.json();
    } catch {
      /* ignore */
    }
    if (res.status === 401) notifyUnauthorized();
    throw errorFromBody(res.status, parsed);
  }
  return res.blob();
}

export const api = {
  version: (signal?: AbortSignal) => request<VersionResponse>("/version", { signal }),
  checkToken: (token: string) => request<StatsResponse>("/stats", { token }),
  stats: (signal?: AbortSignal) => request<StatsResponse>("/stats", { signal }),
  jobs: (query: { status?: string; limit?: number; offset?: number } = {}, signal?: AbortSignal) =>
    request<JobList>("/jobs", { query, signal }),
  job: (id: string, signal?: AbortSignal) => request<JobDetail>(`/jobs/${encodeURIComponent(id)}`, { signal }),
  createJob: (body: JobCreateBody) => request<Job>("/jobs", { method: "POST", body }),
  control: (id: string, action: ControlAction, reason = "") =>
    request<ControlResult>(`/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST", query: { reason } }),
  plan: (id: string, signal?: AbortSignal) => request<PlanVersion[]>(`/jobs/${encodeURIComponent(id)}/plan`, { signal }),
  step: (stepId: string, signal?: AbortSignal) => request<StepDetail>(`/steps/${encodeURIComponent(stepId)}`, { signal }),
  events: async (id: string, query: { after?: number; limit?: number; types?: string } = {}, signal?: AbortSignal) => {
    const raw = await request<unknown[]>(`/jobs/${encodeURIComponent(id)}/events`, { query, signal });
    return raw.map(normalizeEvent).filter((e): e is HermEvent => e !== null);
  },
  artifacts: (id: string, signal?: AbortSignal) => request<Artifact[]>(`/jobs/${encodeURIComponent(id)}/artifacts`, { signal }),
  diff: (id: string, signal?: AbortSignal) => request<DiffResponse>(`/jobs/${encodeURIComponent(id)}/diff`, { signal }),
  research: (id: string, signal?: AbortSignal) => request<ResearchRun[]>(`/jobs/${encodeURIComponent(id)}/research`, { signal }),
  workers: (signal?: AbortSignal) => request<Worker[]>("/workers", { signal }),
  worker: (id: string, signal?: AbortSignal) =>
    request<WorkerDetail>(`/workers/${encodeURIComponent(id)}`, { query: { health_limit: 10 }, signal }),
  models: (signal?: AbortSignal) => request<ModelsResponse>("/models", { signal }),
  resources: (signal?: AbortSignal) => request<ResourcesResponse>("/resources", { signal }),
  repositories: (signal?: AbortSignal) => request<Repository[]>("/repositories", { signal }),
  bugs: (status?: string, signal?: AbortSignal) => request<BugRecord[]>("/bugs", { query: { status }, signal }),
};

export function artifactDownloadPath(id: string): string {
  return `/artifacts/${encodeURIComponent(id)}/download`;
}

export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) return err.message;
    if (err.status === 401) return "Nicht angemeldet oder Token ungültig";
    if (err.status === 403) return `Keine Berechtigung: ${err.message}`;
    if (err.status === 404) return `Nicht gefunden: ${err.message}`;
    if (err.status === 409) return `Konflikt: ${err.message}`;
    return `${err.message} (${err.code})`;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}
