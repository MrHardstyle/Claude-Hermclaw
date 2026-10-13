import { readFileSync } from "node:fs";
import { expect, test } from "./fixtures";

test.describe("Job-Detail", () => {
  test("Kopf, Übersicht und Tabs per Tastatur (31.1/31.4)", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.main}`);
    await expect(page.getByTestId("job-title")).toHaveText("Health-Endpoint erweitern");
    await expect(page.locator(".job-head").getByTestId("job-status")).toHaveText("Läuft");
    await expect(page.getByTestId("status-line")).toContainText("Reviewer meldet Major Finding");
    await expect(page.getByTestId("job-prompt")).toContainText("Erweitere den Health-Endpoint");
    await expect(page.getByTestId("step-counts")).toContainText("Abgeschlossen");

    const tablist = page.getByRole("tablist", { name: "Job-Ansichten" });
    const overview = tablist.getByRole("tab", { name: "Übersicht" });
    await expect(overview).toHaveAttribute("aria-selected", "true");
    await overview.focus();
    await page.keyboard.press("ArrowRight");
    await expect(tablist.getByRole("tab", { name: "Plan" })).toBeFocused();
    await expect(tablist.getByRole("tab", { name: "Plan" })).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(new RegExp(`/jobs/${state.jobs.main}/plan$`));
    await page.keyboard.press("End");
    await expect(tablist.getByRole("tab", { name: /Fehler/ })).toBeFocused();
    await expect(page.getByTestId("panel-errors")).toBeVisible();
  });

  test("Plan-Tab zeigt den DAG mit Status-Farben und Planversionen (31.4)", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.main}/plan`);
    const dag = page.getByTestId("dag");
    await expect(dag.getByTestId("dag-node")).toHaveCount(4);
    await expect(dag.getByTestId("dag-edge")).toHaveCount(4);
    await expect(dag.locator('[data-step="S1"]')).toHaveAttribute("data-status", "completed");
    await expect(dag.locator('[data-step="S3"]')).toHaveAttribute("data-status", "verifying");
    await expect(dag.locator('[data-step="S4"]')).toHaveAttribute("data-status", "pending");
    await expect(dag.locator('[data-step="S1"]')).toHaveClass(/tone-success/);
    await expect(dag.locator('[data-from="S3"][data-to="S4"]')).toHaveCount(1);
    await expect(dag.locator('[data-from="S1"][data-to="S3"]')).toHaveCount(1);
    // dependencies are left of their dependants (topological layers)
    const x = async (key: string) => {
      const t = await dag.locator(`[data-step="${key}"]`).getAttribute("transform");
      return Number(/translate\(([\d.]+)/.exec(t ?? "")?.[1]);
    };
    expect(await x("S1")).toBeLessThan(await x("S2"));
    expect(await x("S2")).toBeLessThan(await x("S3"));
    expect(await x("S3")).toBeLessThan(await x("S4"));

    await expect(page.getByText("Neuplanung nach fehlgeschlagenem Test: eigener Testschritt", { exact: true })).toBeVisible();
    await expect(page.getByText("Test fehlgeschlagen – Neuplanung", { exact: true }).first()).toBeVisible();
    const select = page.getByTestId("plan-version-select");
    await expect(select.locator("option")).toHaveCount(2);
    await select.selectOption("1");
    await expect(dag.getByTestId("dag-node")).toHaveCount(3);
    await expect(dag.getByTestId("dag-edge")).toHaveCount(3);
    await expect(dag.locator('[data-step="S3"]')).toHaveAttribute("data-status", "planned");
    await select.selectOption("2");

    // clicking / Enter on a node opens the step
    await dag.locator('[data-step="S3"]').focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`/jobs/${state.jobs.main}/steps/S3$`));
    await expect(page.getByTestId("step-detail")).toHaveAttribute("data-step", "S3");
  });

  test("Schritt-Detail: Versuche, Scope, Tools, Befehle, Tests, Verifier, Review (31.5)", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.main}/steps/S3`);
    const d = page.getByTestId("step-detail");
    await expect(d.getByTestId("attempt-row")).toHaveCount(2);
    await expect(d.getByTestId("attempts-table")).toContainText("tests_failed");
    await expect(d.getByTestId("scope-versions").locator("li")).toHaveCount(2);
    await expect(d.getByTestId("scope-versions")).toContainText("tests/test_api.py");
    await expect(d.getByTestId("toolcalls-table")).toContainText("apply_patch");
    await expect(d.getByTestId("commands-table")).toContainText("pytest -q tests/test_api.py");
    await expect(d.getByTestId("tests-table").getByTestId("test-row")).toHaveCount(2);
    await expect(d.getByTestId("verifier-check")).toHaveCount(4);
    await expect(d.getByTestId("review-finding")).toHaveCount(2);
    await expect(d.getByText("Version im Health-Endpoint")).toBeVisible();
    // superseded steps are hidden by default and can be shown
    await expect(page.getByTestId("step-row")).toHaveCount(4);
    await page.getByLabel("Ersetzte Schritte anzeigen").check();
    await expect(page.getByTestId("step-row")).toHaveCount(5);
  });

  test("Tests-, Verifier- und Review-Tab aggregieren über alle Schritte (31.7–31.9)", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.main}/tests`);
    await expect(page.getByTestId("tests-passed").locator(".stat-value")).toHaveText("22");
    await expect(page.getByTestId("tests-failed-runs").locator(".stat-value")).toHaveText("1");
    await expect(page.getByTestId("test-row")).toHaveCount(2);
    await expect(page.locator('[data-testid="test-row"][data-status="failed"]')).toContainText("S3");

    await page.getByRole("tab", { name: "Verifier" }).click();
    await expect(page.getByTestId("verifier-checks-total").locator(".stat-value")).toHaveText("4");
    await expect(page.getByTestId("verifier-checks-failed").locator(".stat-value")).toHaveText("1");
    const failedCheck = page.locator('[data-testid="verifier-check"][data-status="fail"]');
    await expect(failedCheck).toContainText("test_health_version fehlgeschlagen");
    // failed blocking checks are listed first
    await expect(page.getByTestId("verifier-check").first()).toHaveAttribute("data-status", "fail");

    await page.getByRole("tab", { name: "Review" }).click();
    await expect(page.getByTestId("review-major").locator(".stat-value")).toHaveText("1");
    await expect(page.getByTestId("review-run")).toHaveAttribute("data-verdict", "fix_required");
    await expect(page.getByText(/Urteil durch Invariante korrigiert/)).toBeVisible();
    await expect(page.getByTestId("review-finding").first()).toHaveAttribute("data-severity", "major");
    await expect(page.getByTestId("review-finding").first()).toContainText("Fehlende Fehlerbehandlung im Health-Endpoint");
  });

  test("Änderungen: Diff-Viewer mit Dateiköpfen und +/- Färbung (31.6)", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.main}/changes`);
    const files = page.getByTestId("diff-file");
    await expect(files).toHaveCount(2);
    await expect(files.nth(0).locator(".diff-path")).toHaveText("src/api.py");
    await expect(files.nth(1).locator(".diff-path")).toHaveText("tests/test_api.py");
    await expect(files.nth(1)).toContainText("neu");
    await expect(page.getByTestId("diff-summary")).toContainText("2 Dateien geändert");
    await expect(files.nth(0).locator(".diff-hunk")).toContainText("@@ -1,6 +1,9 @@");
    const added = files.nth(0).locator("tr.diff-add", { hasText: "from fastapi.responses import JSONResponse" });
    await expect(added).toHaveCount(1);
    const removed = files.nth(0).locator("tr.diff-del", { hasText: 'return {"ok": True}' });
    await expect(removed).toHaveCount(1);
    const addBg = await added.evaluate((el) => getComputedStyle(el).backgroundColor);
    const delBg = await removed.evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(addBg).not.toBe(delBg);
    // collapse/expand a file
    await files.nth(1).getByRole("button", { name: /tests\/test_api.py/ }).click();
    await expect(files.nth(1).locator("table")).toHaveCount(0);
    // raw diff download (authenticated fetch → blob)
    const download = page.waitForEvent("download");
    await page.locator("#changes-main").getByRole("button", { name: /herunterladen/ }).click();
    expect((await download).suggestedFilename()).toMatch(/\.diff$/);
    // complete diff → no truncation notice
    await expect(page.getByTestId("diff-truncated")).toHaveCount(0);
  });

  test("Änderungen: gekürzter Diff wird als unvollständig gekennzeichnet (31.6)", async ({ authed: page, state }) => {
    // real API response, only flagged as truncated (the current API cuts at 2 000 000 characters without a flag)
    await page.route(`**/api/jobs/${state.jobs.main}/diff`, async (route) => {
      const res = await route.fetch();
      const body = (await res.json()) as Record<string, unknown>;
      await route.fulfill({ response: res, json: { ...body, truncated: true } });
    });
    await page.goto(`/jobs/${state.jobs.main}/changes`);
    await expect(page.getByTestId("diff-file")).toHaveCount(2);
    const notice = page.getByTestId("diff-truncated");
    await expect(notice).toBeVisible();
    await expect(notice).toHaveAttribute("role", "status");
    await expect(notice).toContainText("2.000.000 Zeichen");
  });

  test("Research-Quellen: Läufe, Quellen mit Autorität/Relevanz, Aussagen, Widersprüche (31.10)", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.main}/research`);
    await expect(page.getByRole("heading", { name: "Wie streamt FastAPI Server-Sent Events?" })).toBeVisible();
    await expect(page.getByTestId("research-synthesis")).toContainText("EventSourceResponse");
    const rows = page.getByTestId("source-row");
    await expect(rows).toHaveCount(3);
    // sorted by authority, every http(s) source is a clickable external link
    const official = rows.nth(0).getByTestId("source-link");
    await expect(official).toHaveAttribute("href", "https://fastapi.tiangolo.com/advanced/server-sent-events/");
    await expect(official).toHaveAttribute("target", "_blank");
    await expect(official).toHaveAttribute("rel", /noopener/);
    await expect(rows.nth(1).getByTestId("source-link")).toHaveAttribute("href", /developer\.mozilla\.org/);
    await expect(rows.nth(0).getByRole("meter").first()).toHaveAttribute("aria-valuenow", "95");
    // a javascript: URL is never rendered as link
    await expect(rows.nth(2)).toContainText("Untrusted forum post");
    await expect(rows.nth(2).getByTestId("source-link")).toHaveCount(0);
    await expect(page.locator('a[href^="javascript:"]')).toHaveCount(0);

    const claims = page.locator("ul.claims").first().getByTestId("research-claim");
    await expect(claims).toHaveCount(3);
    const first = claims.filter({ hasText: "Last-Event-ID" });
    await expect(first.getByRole("link", { name: /Quelle 1/ })).toHaveAttribute("href", /#source-/);
    await expect(first.getByRole("link", { name: /Quelle 2/ })).toBeVisible();
    await expect(first).toContainText("für Entscheidung genutzt");
    await expect(page.getByTestId("research-contradictions")).toContainText("Gruppe 1");
    await expect(page.getByTestId("research-contradictions").getByTestId("research-claim")).toHaveCount(2);
  });

  test("Repository, Ressourcen, Artefakte, Medien, Git, Fehler (31.12–31.14)", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.main}/repository`);
    await expect(page.locator("#repo-main")).toContainText("demo-repo");
    await expect(page.locator("#repo-main")).toContainText("file:///srv/git/demo-repo.git");

    await page.getByRole("tab", { name: "Ressourcen" }).click();
    await expect(page.getByTestId("job-leases")).toContainText("large-model-224");

    await page.getByRole("tab", { name: "Artefakte" }).click();
    await expect(page.getByTestId("artifact-row")).toHaveCount(3);
    const dl = page.waitForEvent("download");
    await page.getByRole("button", { name: "pytest.log herunterladen" }).click();
    const file = await dl;
    expect(file.suggestedFilename()).toBe("pytest.log");
    // the bytes survive the authenticated blob hand-over (re-typed as opaque attachment)
    const saved = await file.path();
    expect(readFileSync(saved, "utf-8")).toBe("10 passed, 2 failed\n");

    await page.getByRole("tab", { name: "Medien" }).click();
    const img = page.getByTestId("media-grid").getByRole("img", { name: "screenshot.png" });
    await expect(img).toBeVisible();
    await expect(img).toHaveAttribute("src", /^blob:/);
    expect(await img.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBe(8);
    await expect(page.locator("#media-events")).toContainText("media.finished");

    await page.getByRole("tab", { name: /Git/ }).click();
    await expect(page.getByTestId("git-commits")).toContainText("9a8b7c6d5e4f");
    await expect(page.getByTestId("git-commits")).toContainText("feat: health endpoint mit Version");
    await expect(page.locator("#git-events")).toContainText("checkpoint.created");

    await page.getByRole("tab", { name: /Fehler/ }).click();
    const errors = page.getByTestId("error-events");
    await expect(errors).toContainText("LiteLLM Timeout beim Review (wird wiederholt)");
    await expect(errors).toContainText("verifier.check.failed");
    await expect(errors).toContainText("test.failed");
  });

  test("Fehler-Tab eines fehlgeschlagenen Jobs zeigt den Job-Fehler (31.14)", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.failed}/errors`);
    await expect(page.getByTestId("job-error")).toContainText("VERIFY_FAILED");
    await expect(page.getByTestId("job-error")).toContainText("Verifier: 2 blockierende Checks fehlgeschlagen");
    await expect(page.getByTestId("error-events")).toContainText("job.failed");
  });
});
