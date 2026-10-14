import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base, expect, type Page } from "@playwright/test";

const here = path.dirname(fileURLToPath(import.meta.url));
const UI_DIR = path.resolve(here, "../..");
const REPO_ROOT = path.resolve(UI_DIR, "..");
const STATE_FILE = process.env.HERMCLAW_E2E_STATE ?? path.join(UI_DIR, "tests", ".runtime", "state.json");
const PYTHON = process.env.HERMCLAW_E2E_PYTHON ?? path.join(REPO_ROOT, ".venv", "bin", "python");

export interface E2EState {
  token: string;
  api_url: string;
  jobs: { main: string; queued: string; pausable: string; failed: string };
  steps: Record<string, string>;
  ssh_approval: string;
}

export function loadState(): E2EState {
  return JSON.parse(readFileSync(STATE_FILE, "utf-8")) as E2EState;
}

/** Append an event in a separate process (real DB commit → NOTIFY → SSE). Returns its sequence. */
export function emitEvent(jobId: string, text: string, type = "status"): number {
  const out = execFileSync(PYTHON, [path.join(here, "seed.py"), "emit", "--state", STATE_FILE, "--job", jobId, "--text", text, "--type", type], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: REPO_ROOT },
    encoding: "utf-8",
  });
  return (JSON.parse(out.trim()) as { sequence: number }).sequence;
}

/** Direct API call with the test token (to verify server-side state independent of the UI). */
export async function apiGet<T>(p: string): Promise<T> {
  const st = loadState();
  const r = await fetch(`${st.api_url}/api${p}`, { headers: { Authorization: `Bearer ${st.token}` } });
  if (!r.ok) throw new Error(`GET ${p} → ${String(r.status)}`);
  return (await r.json()) as T;
}

export async function loginViaStorage(page: Page, token: string): Promise<void> {
  await page.addInitScript((t) => {
    window.sessionStorage.setItem("hermclaw.apiToken", t);
  }, token);
}

export const test = base.extend<{ state: E2EState; authed: Page }>({
  // eslint-disable-next-line no-empty-pattern
  state: async ({}, use) => {
    await use(loadState());
  },
  authed: async ({ page, state }, use) => {
    await loginViaStorage(page, state.token);
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await use(page);
    expect(errors, "no uncaught page errors").toEqual([]);
  },
});

export { expect };

/** Create a job in a given status directly in the DB (fresh subject for control tests). */
export function makeJob(title: string, status: string): string {
  const out = execFileSync(PYTHON, [path.join(here, "seed.py"), "job", "--state", STATE_FILE, "--title", title, "--status", status], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: REPO_ROOT },
    encoding: "utf-8",
  });
  return (JSON.parse(out.trim()) as { id: string }).id;
}

/** Force a job status directly in the DB (simulates a transition made concurrently by the runtime). */
export function setJobStatus(id: string, status: string): void {
  execFileSync(PYTHON, [path.join(here, "seed.py"), "status", "--state", STATE_FILE, "--job", id, "--status", status], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: REPO_ROOT },
    encoding: "utf-8",
  });
}

/**
 * Real network failures (see net-chaos.ts): side "ui" = browser ↔ UI server, side "api" = UI server proxy ↔ API.
 */
export async function network(
  side: "ui" | "api",
  action: "drop" | "block" | "unblock",
): Promise<{ open: number; accepted: number; blocked: boolean; dropped: number }> {
  const base = side === "ui" ? process.env.HERMCLAW_E2E_CHAOS_UI_URL : process.env.HERMCLAW_E2E_CHAOS_API_URL;
  if (!base) throw new Error(`chaos proxy "${side}" not available (global setup did not start it)`);
  const r = await fetch(`${base}/${action}`, { method: "POST" });
  if (!r.ok) throw new Error(`chaos ${action} → ${String(r.status)}`);
  return (await r.json()) as { open: number; accepted: number; blocked: boolean; dropped: number };
}

/** Direct API POST with the test token (setup/cleanup independent of the UI). */
export async function apiPost<T>(p: string, body: unknown): Promise<T> {
  const st = loadState();
  const r = await fetch(`${st.api_url}/api${p}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${st.token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`POST ${p} → ${String(r.status)}`);
  return (await r.json()) as T;
}
