import { apiGet, expect, makeJob, setJobStatus, test } from "./fixtures";

interface JobOut {
  status: string;
  pause_requested: boolean;
  cancel_requested: boolean;
  metadata: Record<string, unknown>;
}

test.describe("Steuerung (31.15)", () => {
  test("Pausieren und Fortsetzen mit Bestätigung und Statusänderung", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.pausable}`);
    const pause = page.getByTestId("control-pause");
    const resume = page.getByTestId("control-resume");
    await expect(pause).toBeEnabled();
    await expect(resume).toBeDisabled();
    await expect(page.getByTestId("control-retry")).toBeDisabled();

    // dismissing the dialog does nothing
    await pause.click();
    const dialog = page.getByRole("dialog", { name: "Pausieren?" });
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "Zurück" }).click();
    await expect(dialog).toBeHidden();
    expect((await apiGet<JobOut>(`/jobs/${state.jobs.pausable}`)).pause_requested).toBe(false);

    await pause.click();
    await page.getByTestId("confirm-action").click();
    await expect(page.getByTestId("control-message")).toContainText("Pausieren:");
    await expect(page.locator(".controls").getByText("Pause angefordert")).toBeVisible();
    await expect(pause).toBeDisabled();
    await expect(resume).toBeEnabled();
    expect((await apiGet<JobOut>(`/jobs/${state.jobs.pausable}`)).pause_requested).toBe(true);

    await resume.click();
    await page.getByTestId("confirm-action").click();
    await expect(page.locator(".controls").getByText("Pause angefordert")).toHaveCount(0);
    await expect(pause).toBeEnabled();
    expect((await apiGet<JobOut>(`/jobs/${state.jobs.pausable}`)).pause_requested).toBe(false);
    // the control action is visible as event
    await page.getByRole("tab", { name: /Live-Events/ }).click();
    await expect(page.locator('[data-type="job.control.resume"]')).toHaveCount(1);
  });

  test("Neuplanung mit Begründung", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.pausable}`);
    await page.getByTestId("control-replan").click();
    const dialog = page.getByRole("dialog", { name: "Neu planen?" });
    await dialog.getByLabel("Begründung (optional)").fill("Scope zu eng");
    await page.getByTestId("confirm-action").click();
    await expect(page.getByTestId("control-message")).toContainText("replan requested");
    expect((await apiGet<JobOut>(`/jobs/${state.jobs.pausable}`)).metadata.replan_requested).toBe("Scope zu eng");
  });

  test("Abbrechen eines wartenden Jobs beendet ihn sofort; danach sind die Controls gesperrt", async ({ authed: page }) => {
    const id = makeJob("Abbruch-Kandidat", "queued");
    await page.goto(`/jobs/${id}`);
    await page.getByTestId("control-cancel").click();
    const dialog = page.getByRole("dialog", { name: "Abbrechen?" });
    await expect(dialog).toContainText("wirklich abbrechen");
    await page.getByTestId("confirm-action").click();
    await expect(page.locator(".job-head").getByTestId("job-status")).toHaveText("Abgebrochen");
    for (const a of ["cancel", "pause", "resume", "retry", "replan"]) await expect(page.getByTestId(`control-${a}`)).toBeDisabled();
    await expect(page.getByTestId("control-cancel")).toHaveAttribute("title", "Job ist bereits beendet");
    expect((await apiGet<JobOut>(`/jobs/${id}`)).status).toBe("cancelled");
  });

  test("Erneut versuchen stellt einen fehlgeschlagenen Job wieder in die Warteschlange", async ({ authed: page }) => {
    const id = makeJob("Retry-Kandidat", "failed");
    await page.goto(`/jobs/${id}`);
    await expect(page.getByTestId("control-pause")).toBeDisabled();
    const retry = page.getByTestId("control-retry");
    await expect(retry).toBeEnabled();
    await retry.click();
    await page.getByTestId("confirm-action").click();
    await expect(page.locator(".job-head").getByTestId("job-status")).toHaveText("In Warteschlange");
    await expect(retry).toBeDisabled();
    expect((await apiGet<JobOut>(`/jobs/${id}`)).status).toBe("queued");
  });

  test("Serverkonflikt wird im Dialog angezeigt", async ({ authed: page }) => {
    const id = makeJob("Konflikt-Kandidat", "running");
    await page.goto(`/jobs/${id}`);
    await page.getByTestId("control-pause").click();
    // meanwhile the runtime finishes the job → the server answers 409
    setJobStatus(id, "succeeded");
    await page.getByTestId("confirm-action").click();
    const alert = page.getByRole("dialog").getByRole("alert");
    await expect(alert).toContainText("Konflikt");
    await expect(alert).toContainText("succeeded");
    // the job is reloaded behind the open dialog: the action is no longer allowed
    await expect(page.locator(".job-head").getByTestId("job-status")).toHaveText("Erfolgreich");
    await expect(page.getByTestId("confirm-action")).toBeDisabled();
    await page.getByRole("dialog").getByRole("button", { name: "Zurück" }).click();
    await expect(page.getByRole("dialog")).toBeHidden();
    await expect(page.getByTestId("control-pause")).toBeDisabled();
    expect((await apiGet<JobOut>(`/jobs/${id}`)).status).toBe("succeeded");
  });
});
