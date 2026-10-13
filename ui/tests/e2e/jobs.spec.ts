import { apiGet, expect, test } from "./fixtures";

interface JobOut {
  id: string;
  title: string;
  status: string;
  priority: number;
  base_branch: string | null;
  repository_id: string | null;
  metadata: Record<string, unknown>;
}

test.describe("Jobs (31.3)", () => {
  test("Liste zeigt die Jobs und filtert nach Status", async ({ authed: page }) => {
    await page.goto("/jobs");
    const table = page.getByTestId("jobs-table");
    await expect(table.getByRole("link", { name: "Health-Endpoint erweitern" })).toBeVisible();
    await expect(table.getByRole("link", { name: "Fehlgeschlagener Migrationsjob" })).toBeVisible();

    await page.getByTestId("status-filter").selectOption("failed");
    await expect(page).toHaveURL(/status=failed/);
    await expect(table.getByRole("link", { name: "Fehlgeschlagener Migrationsjob" })).toBeVisible();
    await expect(table.getByRole("link", { name: "Health-Endpoint erweitern" })).toHaveCount(0);
    for (const badge of await table.getByTestId("job-status").all()) await expect(badge).toHaveText("Fehlgeschlagen");
  });

  test("Formular validiert Eingaben und legt einen Job über die API an", async ({ authed: page }) => {
    await page.goto("/jobs");
    await page.getByTestId("toggle-job-form").click();
    const form = page.getByTestId("job-form");

    // client-side validation
    await form.getByLabel("Aufgabe (Prompt) *").fill("x");
    await form.getByLabel("URL").fill("ftp://nope");
    await form.getByLabel("Priorität (0–100)").fill("101");
    await page.getByTestId("job-submit").click();
    await expect(form.getByText("Bitte eine Aufgabenbeschreibung mit mindestens 3 Zeichen eingeben.")).toBeVisible();
    await expect(form.getByText("URL muss mit ssh://, git@, http(s)://, file:// oder / (lokaler Pfad) beginnen.")).toBeVisible();
    await expect(form.getByText("Priorität muss eine ganze Zahl zwischen 0 und 100 sein.")).toBeVisible();
    await expect(form.getByLabel("Aufgabe (Prompt) *")).toHaveAttribute("aria-invalid", "true");

    // valid submission
    const title = `UI-Job ${String(Date.now())}`;
    await form.getByLabel("Aufgabe (Prompt) *").fill("Füge einen /version-Endpoint hinzu und dokumentiere ihn.");
    await form.getByLabel("Titel").fill(title);
    await form.getByLabel("Name").fill("demo-repo");
    await form.getByLabel("URL").fill("");
    await form.getByLabel("Basis-Branch").fill("develop");
    await form.getByLabel("Vorgaben (eine pro Zeile)").fill("Keine Breaking Changes\n\n  Tests ergänzen  ");
    await form.getByLabel("Priorität (0–100)").fill("80");
    await form.getByLabel("Web-Research erlauben").uncheck();
    await form.getByLabel("Merge Request").selectOption("yes");
    await page.getByTestId("job-submit").click();

    await expect(page.getByTestId("job-created")).toContainText(title);
    const row = page.getByTestId("jobs-table").getByRole("row", { name: new RegExp(title) });
    await expect(row).toBeVisible();
    await expect(row.getByTestId("job-status")).toHaveText("In Warteschlange");
    await expect(row).toContainText("demo-repo");
    await expect(row).toContainText("develop");

    // server-side state matches the form
    const jobId = await row.getAttribute("data-job");
    expect(jobId).toBeTruthy();
    const job = await apiGet<JobOut>(`/jobs/${jobId ?? ""}`);
    expect(job.title).toBe(title);
    expect(job.priority).toBe(80);
    expect(job.base_branch).toBe("develop");
    expect(job.repository_id).not.toBeNull();
    expect(job.metadata).toMatchObject({ allow_network_research: false, auto_commit: true, create_merge_request: true });

    // the created job opens from the success message
    await page.getByTestId("job-created").getByRole("link", { name: title }).click();
    await expect(page.getByTestId("job-title")).toHaveText(title);
    await expect(page.getByTestId("status-line")).toContainText("Job angelegt und in die Warteschlange gestellt");
  });

  test("Serverfehler werden angezeigt (unbekanntes Repository ohne URL)", async ({ authed: page }) => {
    await page.goto("/jobs?new=1");
    const form = page.getByTestId("job-form");
    await form.getByLabel("Aufgabe (Prompt) *").fill("Etwas im unbekannten Repository ändern");
    await form.getByLabel("Name").fill("gibt-es-nicht");
    await page.getByTestId("job-submit").click();
    await expect(page.getByTestId("job-form-error")).toContainText("not registered");
  });
});
