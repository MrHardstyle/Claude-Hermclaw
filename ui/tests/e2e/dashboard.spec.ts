import { emitEvent, expect, makeJob, test } from "./fixtures";

test.describe("Dashboard (31.2)", () => {
  test("zeigt Jobs, Warteschlange, Worker, Modelle, GPU, Fehler, Research und Bugs", async ({ authed: page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();

    // jobs by status (4 seeded jobs, other tests may add more)
    await expect(page.getByTestId("stat-jobs-total").locator(".stat-value")).toHaveText(/^\d+$/);
    const total = Number(await page.getByTestId("stat-jobs-total").locator(".stat-value").textContent());
    expect(total).toBeGreaterThanOrEqual(4);
    const chips = page.getByTestId("jobs-by-status");
    await expect(chips.getByText("Läuft")).toBeVisible();
    await expect(chips.getByText("Fehlgeschlagen")).toBeVisible();

    // queue
    await expect(page.getByTestId("queue-list").getByRole("link", { name: "Doku-Job in Warteschlange" })).toBeVisible();

    // workers with state badges
    const workers = page.getByTestId("dashboard-workers");
    await expect(workers.locator('[data-worker="coder-222"] [data-testid="worker-state"]')).toHaveText("BUSY");
    await expect(workers.locator('[data-worker="model-224"] [data-testid="worker-state"]')).toHaveText("READY");
    await expect(workers.locator('[data-worker="media-224"] [data-testid="worker-state"]')).toHaveText("SLEEPING");
    await expect(workers.locator('[data-worker="spare-225"] [data-testid="worker-state"]')).toHaveText("OFFLINE");

    // model profiles + stats (2 seeded planner invocations, 1 failed coder invocation)
    const models = page.getByTestId("models-table");
    const planner = models.getByRole("row", { name: /planner-gemma\b(?!-)/ });
    await expect(planner.getByRole("cell").nth(4)).toHaveText("2");
    const coderRow = models.getByRole("row", { name: /coder-main/ });
    await expect(coderRow.getByRole("cell").nth(6)).toHaveText("1");

    // GPU / resources: the coder lease weighs 23 GB on the model host
    await expect(page.getByTestId("gpu-used")).toHaveText("23");
    await expect(page.getByTestId("stat-leases").locator(".stat-value")).toHaveText("1");
    await expect(page.getByTestId("stat-waiting").locator(".stat-value")).toHaveText("1");

    // recent errors
    await expect(page.getByTestId("failed-jobs").getByText("Verifier: 2 blockierende Checks fehlgeschlagen").first()).toBeVisible();

    // research + bugs
    await expect(page.getByTestId("stat-research-runs").locator(".stat-value")).toHaveText("1");
    await expect(page.getByTestId("stat-research-sources").locator(".stat-value")).toHaveText("3");
    await expect(page.getByTestId("stat-bugs-open").locator(".stat-value")).toHaveText("1");
    await expect(page.getByTestId("dashboard-bugs").getByText("SSE-Verbindung hinter Proxy gepuffert")).toBeVisible();
  });

  test("Live-Statuszeilen kommen über den globalen SSE-Stream", async ({ authed: page }) => {
    await page.goto("/");
    await expect(page.getByTestId("global-stream-state")).toHaveText("Live verbunden");
    // a dedicated job: live lines must not leak into the seeded main job other specs assert on
    const jobId = makeJob("Dashboard-Live-Job", "running");
    const text = `Dashboard-Live ${String(Date.now())}`;
    emitEvent(jobId, text);
    await expect(page.getByTestId("dashboard-status-lines").getByText(text)).toBeVisible();
    // a live error event shows up under "Letzte Fehler"
    const err = `Dashboard-Fehler ${String(Date.now())}`;
    emitEvent(jobId, err, "error");
    await expect(page.getByTestId("dashboard-errors").getByText(err)).toBeVisible();
  });
});
