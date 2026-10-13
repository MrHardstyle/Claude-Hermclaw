import { expect, test } from "./fixtures";

test.describe("Anmeldung (31.1)", () => {
  test("ungültiges Token wird abgewiesen, gültiges meldet an, Abmelden kehrt zur Anmeldung zurück", async ({ page, state }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Hermclaw Next" })).toBeVisible();
    const tokenField = page.getByLabel("API-Token");
    await tokenField.fill("falsches-token");
    await page.getByRole("button", { name: "Anmelden" }).click();
    await expect(page.getByTestId("login-error")).toHaveText("Token ungültig.");

    await tokenField.fill(state.token);
    await page.getByRole("button", { name: "Anmelden" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Dashboard" })).toBeVisible();
    // token is kept in sessionStorage only
    expect(await page.evaluate(() => window.sessionStorage.getItem("hermclaw.apiToken"))).toBe(state.token);
    expect(await page.evaluate(() => window.localStorage.length)).toBe(0);

    // layout: navigation landmarks and active link
    const nav = page.getByRole("navigation", { name: "Hauptnavigation" });
    await expect(nav.getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
    await nav.getByRole("link", { name: "Jobs" }).click();
    await expect(page).toHaveURL(/\/jobs$/);
    await expect(nav.getByRole("link", { name: "Jobs" })).toHaveAttribute("aria-current", "page");

    await page.getByTestId("logout").click();
    await expect(page.getByText("Abgemeldet.")).toBeVisible();
    expect(await page.evaluate(() => window.sessionStorage.getItem("hermclaw.apiToken"))).toBeNull();
  });

  test("abgelaufenes Token führt zurück zur Anmeldung", async ({ page }) => {
    await page.addInitScript(() => {
      window.sessionStorage.setItem("hermclaw.apiToken", "revoked-token");
    });
    await page.goto("/jobs");
    await expect(page.getByText("Sitzung abgelaufen oder Token ungültig – bitte erneut anmelden.")).toBeVisible();
    await expect(page.getByLabel("API-Token")).toBeVisible();
  });

  test("Skip-Link springt zum Hauptinhalt", async ({ authed: page }) => {
    await page.goto("/");
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: "Zum Inhalt springen" });
    await expect(skip).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/#main$/);
  });
});
