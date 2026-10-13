import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";

/** The page itself must not scroll horizontally; wide content (tables, DAG, diff) scrolls in its own container. */
async function expectNoHorizontalPageScroll(page: Page): Promise<void> {
  const { scroll, client } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  expect(scroll, "document scrollWidth vs. viewport").toBeLessThanOrEqual(client + 1);
}

test.describe("Responsives Layout (Mobilgerät)", () => {
  test("Menü-Schalter öffnet die Navigation und schließt sie nach der Navigation", async ({ authed: page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
    const toggle = page.getByRole("button", { name: "Menü" });
    const nav = page.getByRole("navigation", { name: "Hauptnavigation" });
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(nav).toBeHidden();
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(nav).toBeVisible();
    await nav.getByRole("link", { name: "Jobs", exact: true }).click();
    await expect(page).toHaveURL(/\/jobs$/);
    await expect(nav).toBeHidden();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  test("keine horizontale Seiten-Scrollbar auf Dashboard, Jobs, Job-Detail, Worker und Ressourcen", async ({ authed: page, state }) => {
    for (const url of [
      "/",
      "/jobs",
      `/jobs/${state.jobs.main}`,
      `/jobs/${state.jobs.main}/plan`,
      `/jobs/${state.jobs.main}/changes`,
      `/jobs/${state.jobs.main}/events`,
      "/workers?worker=model-224",
      "/resources",
    ]) {
      await page.goto(url);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator(".loading")).toHaveCount(0);
      await expectNoHorizontalPageScroll(page);
    }
    // the DAG keeps its own scroll container instead of widening the page
    await page.goto(`/jobs/${state.jobs.main}/plan`);
    await expect(page.getByTestId("dag")).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });
});
