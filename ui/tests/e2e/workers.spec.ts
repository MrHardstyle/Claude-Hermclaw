import { apiGet, apiPost, expect, test } from "./fixtures";

interface WorkerOut {
  id: string;
  state: string;
  admin_drain?: boolean;
}

test.describe("Worker (31.11)", () => {
  test("Liste mit Zustands-Badges, Detail mit GPU und geladenen Modellen", async ({ authed: page }) => {
    await page.goto("/workers");
    await expect(page.getByRole("heading", { level: 1, name: "Worker" })).toBeVisible();
    const rows = page.getByTestId("worker-row");
    await expect(rows).toHaveCount(4);
    const badge = (id: string) => page.locator(`[data-testid="worker-row"][data-worker="${id}"]`).getByTestId("worker-state");
    await expect(badge("coder-222")).toHaveText("BUSY");
    await expect(badge("model-224")).toHaveText("READY");
    await expect(badge("media-224")).toHaveText("SLEEPING");
    await expect(badge("spare-225")).toHaveText("OFFLINE");
    const coder = page.locator('[data-testid="worker-row"][data-worker="coder-222"]');
    await expect(coder).toContainText("code.python");
    await expect(coder).toContainText("192.168.178.222");
    // the busy worker links to its active job
    await expect(coder.getByRole("link")).toHaveAttribute("href", /\/jobs\/[0-9a-f-]{36}$/);

    // detail: health sample, GPU card, loaded models (admin view)
    const button = page.getByRole("button", { name: "model-224" });
    await button.click();
    await expect(button).toHaveAttribute("aria-pressed", "true");
    await expect(page).toHaveURL(/\/workers\?worker=model-224$/);
    const detail = page.getByTestId("worker-detail");
    await expect(detail).toContainText("Ja"); // protocol compatible
    await expect(detail.getByTestId("gpu-card")).toContainText("RTX 3090");
    await expect(detail.getByTestId("gpu-card")).toContainText("VRAM 19.456 / 24.576 MB");
    await expect(detail.getByTestId("gpu-card")).toContainText("Auslastung 71 %");
    await expect(detail.getByTestId("loaded-models")).toContainText("gemma4:26b");
    await expect(detail.getByRole("meter", { name: "VRAM GPU 0" })).toBeVisible();

    // toggling the selection closes the detail again
    await button.click();
    await expect(page.getByTestId("worker-detail")).toHaveCount(0);
  });

  test("Drain mit Bestätigung setzt den Worker auf DRAINING und wieder zurück", async ({ authed: page }) => {
    try {
      await page.goto("/workers?worker=model-224");
      const detail = page.getByTestId("worker-detail");
      await expect(detail).toBeVisible();
      await page.getByTestId("worker-drain").click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toContainText("erhält keine neuen Aufgaben mehr");
      await dialog.getByLabel("Begründung (optional)").fill("Wartung GPU-Treiber");
      await page.getByTestId("confirm-action").click();
      await expect(dialog).toBeHidden();
      const row = page.locator('[data-testid="worker-row"][data-worker="model-224"]');
      await expect(row.getByTestId("worker-state")).toHaveText("DRAINING");
      await expect(detail).toContainText("ja (keine neuen Aufgaben)");
      const after = await apiGet<WorkerOut>("/workers/model-224");
      expect(after.state).toBe("draining");
      expect(after.admin_drain).toBe(true);

      await expect(page.getByTestId("worker-drain")).toHaveText("Drain beenden");
      await page.getByTestId("worker-drain").click();
      await page.getByTestId("confirm-action").click();
      await expect(row.getByTestId("worker-state")).toHaveText("READY");
      expect((await apiGet<WorkerOut>("/workers/model-224")).admin_drain).toBe(false);
    } finally {
      // never leave the shared seed worker drained
      await apiPost("/workers/model-224/drain", { drain: false, reason: "e2e cleanup" });
    }
  });
});
