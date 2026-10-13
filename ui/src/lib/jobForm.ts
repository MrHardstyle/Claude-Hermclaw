/**
 * Create-job form → `POST /api/jobs` body (hermclaw/contracts/job.py::JobCreate).
 * Client-side validation mirrors the server limits so most mistakes are caught before the request;
 * the server stays authoritative (its validation errors are shown as well).
 */
import type { JobCreateBody, RepositoryRef } from "../api/types";

export interface JobFormValues {
  prompt: string;
  title: string;
  repoName: string;
  repoUrl: string;
  baseBranch: string;
  constraints: string;
  priority: string;
  allowNetworkResearch: boolean;
  autoCommit: boolean;
  createMergeRequest: "default" | "yes" | "no";
}

export const EMPTY_JOB_FORM: JobFormValues = {
  prompt: "",
  title: "",
  repoName: "",
  repoUrl: "",
  baseBranch: "",
  constraints: "",
  priority: "50",
  allowNetworkResearch: true,
  autoCommit: true,
  createMergeRequest: "default",
};

export type JobFormErrors = Partial<Record<keyof JobFormValues, string>>;

const REPO_URL_RE = /^(ssh:\/\/|git@|https?:\/\/|file:\/\/)/;
/** git check-ref-format rules (subset relevant for branch names typed by users). */
export function isValidBranchName(name: string): boolean {
  if (!name || name.length > 200) return false;
  for (const ch of name) {
    const code = ch.charCodeAt(0);
    if (code < 0x20 || code === 0x7f) return false;
  }
  if (/[\s~^:?*[\\]/.test(name)) return false;
  if (name.startsWith("/") || name.endsWith("/") || name.endsWith(".") || name.endsWith(".lock")) return false;
  if (name.includes("//") || name.includes("..") || name.includes("@{") || name === "@") return false;
  if (name.startsWith("-") || name.split("/").some((part) => part.startsWith("."))) return false;
  return true;
}

export const LIMITS = { prompt: 50_000, title: 200, constraints: 50, constraintLength: 2000 } as const;

export function parseConstraints(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((c) => c.trim())
    .filter((c) => c.length > 0);
}

export function buildJobCreateBody(v: JobFormValues): { body: JobCreateBody | null; errors: JobFormErrors } {
  const errors: JobFormErrors = {};
  const prompt = v.prompt.trim();
  if (prompt.length < 3) errors.prompt = "Bitte eine Aufgabenbeschreibung mit mindestens 3 Zeichen eingeben.";
  else if (prompt.length > LIMITS.prompt) errors.prompt = `Höchstens ${LIMITS.prompt} Zeichen.`;

  const title = v.title.trim();
  if (title.length > LIMITS.title) errors.title = `Höchstens ${LIMITS.title} Zeichen.`;

  const priority = Number(v.priority.trim());
  if (!/^\d+$/.test(v.priority.trim()) || !Number.isInteger(priority) || priority < 0 || priority > 100) {
    errors.priority = "Priorität muss eine ganze Zahl zwischen 0 und 100 sein.";
  }

  const repoName = v.repoName.trim();
  const repoUrl = v.repoUrl.trim();
  const baseBranch = v.baseBranch.trim();
  if (repoUrl && !REPO_URL_RE.test(repoUrl)) errors.repoUrl = "URL muss mit ssh://, git@, http(s):// oder file:// beginnen.";
  if (baseBranch && !isValidBranchName(baseBranch)) errors.baseBranch = "Ungültiger Branch-Name.";
  if (baseBranch && !repoName && !repoUrl) errors.baseBranch = "Basis-Branch nur zusammen mit einem Repository angeben.";

  const constraints = parseConstraints(v.constraints);
  if (constraints.length > LIMITS.constraints) errors.constraints = `Höchstens ${LIMITS.constraints} Vorgaben (eine pro Zeile).`;
  else if (constraints.some((c) => c.length > LIMITS.constraintLength))
    errors.constraints = `Eine Vorgabe darf höchstens ${LIMITS.constraintLength} Zeichen lang sein.`;

  if (Object.keys(errors).length) return { body: null, errors };

  let repository: RepositoryRef | undefined;
  if (repoName || repoUrl) {
    repository = {};
    if (repoName) repository.name = repoName;
    if (repoUrl) repository.url = repoUrl;
    if (baseBranch) repository.base_branch = baseBranch;
  }
  const body: JobCreateBody = {
    prompt,
    constraints,
    priority,
    allow_network_research: v.allowNetworkResearch,
    auto_commit: v.autoCommit,
  };
  if (title) body.title = title;
  if (repository) body.repository = repository;
  if (v.createMergeRequest !== "default") body.create_merge_request = v.createMergeRequest === "yes";
  return { body, errors };
}
