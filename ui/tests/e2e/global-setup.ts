/**
 * Playwright global setup: real backend + real database + built UI.
 *
 * 1. `seed.py prepare` – fresh PostgreSQL database (migrations applied) and a data dir for artifacts
 * 2. `vite build` (skip with HERMCLAW_UI_SKIP_BUILD=1)
 * 3. FastAPI via uvicorn (`hermclaw.api.app:create_app --factory`) with HERMCLAW_ENV=test and a literal API token
 * 4. `seed.py seed` – one job through POST /api/jobs, the rest through the hermclaw ORM
 * 5. `vite preview` with the /api proxy to the API
 * 6. two controllable TCP proxies (net-chaos.ts) so tests can break real connections:
 *    browser → [chaos "ui", HERMCLAW_E2E_UI_PORT] → vite preview → [chaos "api"] → uvicorn
 * The returned teardown stops all servers and drops the database.
 */
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { createWriteStream, existsSync, mkdirSync, readFileSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { startChaosProxy, type ChaosProxy } from "./net-chaos";

const here = path.dirname(fileURLToPath(import.meta.url));
export const UI_DIR = path.resolve(here, "../..");
export const REPO_ROOT = path.resolve(UI_DIR, "..");
export const RUNTIME_DIR = path.join(UI_DIR, "tests", ".runtime");
export const STATE_FILE = path.join(RUNTIME_DIR, "state.json");
export const PYTHON = process.env.HERMCLAW_E2E_PYTHON ?? path.join(REPO_ROOT, ".venv", "bin", "python");
export const SEED = path.join(here, "seed.py");

function pyEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  return { ...process.env, PYTHONPATH: REPO_ROOT, PYTHONUNBUFFERED: "1", ...extra };
}

function run(cmd: string, args: string[], env: NodeJS.ProcessEnv, label: string): void {
  const r = spawnSync(cmd, args, { cwd: REPO_ROOT, env, encoding: "utf-8", timeout: 300_000 });
  if (r.status !== 0) {
    throw new Error(`${label} failed (exit ${String(r.status)}):\n${r.stdout}\n${r.stderr}`);
  }
}

async function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once("error", () => {
      resolve(false);
    });
    srv.listen(port, "127.0.0.1", () => {
      srv.close(() => {
        resolve(true);
      });
    });
  });
}

async function waitFor(url: string, child: ChildProcess, label: string, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastErr = "";
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`${label} exited early with code ${String(child.exitCode)} (see tests/.runtime/*.log)`);
    try {
      const r = await fetch(url);
      if (r.ok) return;
      lastErr = `HTTP ${String(r.status)}`;
    } catch (e) {
      lastErr = String(e);
    }
    await new Promise((res) => setTimeout(res, 250));
  }
  throw new Error(`${label} not ready at ${url}: ${lastErr}`);
}

function startProcess(cmd: string, args: string[], env: NodeJS.ProcessEnv, cwd: string, logName: string): ChildProcess {
  const log = createWriteStream(path.join(RUNTIME_DIR, logName), { flags: "w" });
  const child = spawn(cmd, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"], detached: false });
  child.stdout?.pipe(log);
  child.stderr?.pipe(log);
  return child;
}

async function stopProcess(child: ChildProcess | undefined): Promise<void> {
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  const exited = await Promise.race([
    new Promise<boolean>((res) => child.once("exit", () => res(true))),
    new Promise<boolean>((res) => setTimeout(() => res(false), 8000)),
  ]);
  if (!exited) child.kill("SIGKILL");
}

export default async function globalSetup(): Promise<() => Promise<void>> {
  const apiPort = Number(process.env.HERMCLAW_E2E_API_PORT ?? 18765);
  const uiPort = Number(process.env.HERMCLAW_E2E_UI_PORT ?? 14173);
  const vitePort = Number(process.env.HERMCLAW_E2E_VITE_PORT ?? uiPort + 1);
  // chaos proxy "api" listens on chaosPort, control endpoints on chaosPort+1 ("api") and chaosPort+2 ("ui")
  const chaosPort = Number(process.env.HERMCLAW_E2E_CHAOS_PORT ?? 18766);
  for (const [p, what] of [
    [apiPort, "HERMCLAW_E2E_API_PORT"],
    [uiPort, "HERMCLAW_E2E_UI_PORT"],
    [vitePort, "HERMCLAW_E2E_VITE_PORT"],
    [chaosPort, "HERMCLAW_E2E_CHAOS_PORT"],
    [chaosPort + 1, "HERMCLAW_E2E_CHAOS_PORT (+1)"],
    [chaosPort + 2, "HERMCLAW_E2E_CHAOS_PORT (+2)"],
  ] as const) {
    if (!(await portFree(p))) throw new Error(`port ${String(p)} is in use – set ${what} to a free port`);
  }
  if (!existsSync(PYTHON)) throw new Error(`python not found at ${PYTHON} (set HERMCLAW_E2E_PYTHON)`);
  mkdirSync(RUNTIME_DIR, { recursive: true });

  run(PYTHON, [SEED, "prepare", "--state", STATE_FILE, "--data-dir", path.join(RUNTIME_DIR, "data")], pyEnv(), "seed.py prepare");
  const state = JSON.parse(readFileSync(STATE_FILE, "utf-8")) as { token: string; database_url: string; data_dir: string };

  let api: ChildProcess | undefined;
  let ui: ChildProcess | undefined;
  let chaosApi: ChaosProxy | undefined;
  let chaosUi: ChaosProxy | undefined;
  const teardown = async () => {
    await chaosUi?.close();
    await stopProcess(ui);
    await chaosApi?.close();
    await stopProcess(api);
    if (!process.env.HERMCLAW_E2E_KEEP_DB) run(PYTHON, [SEED, "teardown", "--state", STATE_FILE], pyEnv(), "seed.py teardown");
  };

  try {
    if (!process.env.HERMCLAW_UI_SKIP_BUILD) {
      run(process.execPath, [path.join(UI_DIR, "node_modules", "vite", "bin", "vite.js"), "build", UI_DIR], { ...process.env }, "vite build");
    }
    const apiEnv = pyEnv({
      HERMCLAW_DATABASE_URL: state.database_url,
      HERMCLAW_ENV: "test",
      HERMCLAW_API_TOKEN_REF: `literal:${state.token}`,
      HERMCLAW_DATA_DIR: state.data_dir,
      HERMCLAW_LOG_LEVEL: "WARNING",
    });
    // --no-access-log: the SSE URL carries ?access_token=… and must not end up in logs
    api = startProcess(
      PYTHON,
      ["-m", "uvicorn", "hermclaw.api.app:create_app", "--factory", "--host", "127.0.0.1", "--port", String(apiPort), "--no-access-log"],
      apiEnv,
      REPO_ROOT,
      "api.log",
    );
    const apiUrl = `http://127.0.0.1:${String(apiPort)}`;
    await waitFor(`${apiUrl}/api/health`, api, "API");

    run(PYTHON, [SEED, "seed", "--state", STATE_FILE, "--api-url", apiUrl], pyEnv(), "seed.py seed");

    chaosApi = await startChaosProxy(apiPort, chaosPort, chaosPort + 1);
    chaosUi = await startChaosProxy(vitePort, uiPort, chaosPort + 2);
    // workers inherit the environment of the runner process
    process.env.HERMCLAW_E2E_CHAOS_API_URL = chaosApi.controlUrl;
    process.env.HERMCLAW_E2E_CHAOS_UI_URL = chaosUi.controlUrl;
    ui = startProcess(
      process.execPath,
      [path.join(UI_DIR, "node_modules", "vite", "bin", "vite.js"), "preview", "--host", "127.0.0.1", "--port", String(vitePort), "--strictPort"],
      { ...process.env, HERMCLAW_UI_API_TARGET: `http://127.0.0.1:${String(chaosApi.port)}`, HERMCLAW_UI_PORT: String(vitePort) },
      UI_DIR,
      "ui.log",
    );
    await waitFor(`http://127.0.0.1:${String(uiPort)}/`, ui, "vite preview");
    // the proxy must reach the API as well
    await waitFor(`http://127.0.0.1:${String(uiPort)}/api/health`, ui, "vite preview proxy");
  } catch (e) {
    await teardown();
    throw e;
  }
  process.env.HERMCLAW_E2E_STATE = STATE_FILE;
  return teardown;
}
