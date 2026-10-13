import { apiGet, expect, test } from "./fixtures";

test.describe("Navigation (31.1)", () => {
  test("Hauptnavigation markiert die aktive Seite und setzt den Fokus auf den Inhalt", async ({ authed: page }) => {
    await page.goto("/");
    const nav = page.getByRole("navigation", { name: "Hauptnavigation" });
    for (const [label, url, heading] of [
      ["Jobs", /\/jobs$/, "Jobs"],
      ["Worker", /\/workers$/, "Worker"],
      ["Ressourcen", /\/resources$/, "Ressourcen"],
      ["Medien", /\/media$/, "Medien"],
      ["Bugs", /\/bugs$/, "Bugs"],
      ["Dashboard", /\/$/, "Dashboard"],
    ] as const) {
      await nav.getByRole("link", { name: label, exact: true }).click();
      await expect(page).toHaveURL(url);
      await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
      await expect(nav.getByRole("link", { name: label, exact: true })).toHaveAttribute("aria-current", "page");
      await expect(page.locator("main#main")).toBeFocused();
    }
    // unknown routes render the not-found page inside the layout
    await page.goto("/gibt-es-nicht");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("nicht gefunden");
  });
});

test.describe("Ressourcen (31.12)", () => {
  test("aktive Leases und wartende Anfragen mit Job-Verweisen", async ({ authed: page, state }) => {
    await page.goto("/resources");
    await expect(page.getByRole("heading", { level: 1, name: "Ressourcen" })).toBeVisible();
    const res = await apiGet<{ active_leases: { resource: string }[]; waiting_requests: { resource: string }[] }>("/resources");
    await expect(page.getByTestId("res-active").locator(".stat-value")).toHaveText(String(res.active_leases.length));
    await expect(page.getByTestId("res-waiting").locator(".stat-value")).toHaveText(String(res.waiting_requests.length));
    const lease = page.getByTestId("lease-row").first();
    await expect(lease).toContainText("coder");
    await expect(lease).toContainText("runtime-e2e");
    const waiting = page.getByTestId("waiting-row").first();
    await expect(waiting).toContainText("gpu-224");
    await expect(waiting).toContainText("media");
    // GPU usage of the model host is derived from leases and model profiles
    await expect(page.getByTestId("res-used").locator(".stat-value")).toHaveText(/GB$/);
    await expect(page.getByRole("meter", { name: "Modell-Host-Speicher belegt" })).toBeVisible();

    // the lease links to the resources tab of its job
    await lease.getByRole("link").click();
    await expect(page).toHaveURL(new RegExp(`/jobs/${state.jobs.main}/resources$`));
    await expect(page.getByTestId("panel-resources")).toContainText("runtime-e2e");
  });
});

test.describe("Medien (31.13)", () => {
  test("Job wählen zeigt Bild-Vorschau über die Artefakt-Download-URL und Medien-Events", async ({ authed: page, state }) => {
    await page.goto("/media");
    await expect(page.getByText("Einen Job wählen, um seine Bilder und Videos anzusehen.")).toBeVisible();
    const downloads: string[] = [];
    page.on("request", (r) => {
      if (r.url().includes("/api/artifacts/")) downloads.push(r.url());
    });
    await page.getByTestId("media-job-select").selectOption(state.jobs.main);
    await expect(page).toHaveURL(new RegExp(`/media\\?job=${state.jobs.main}$`));
    const grid = page.getByTestId("media-grid");
    const img = grid.getByRole("img", { name: "screenshot.png" });
    await expect(img).toBeVisible();
    // the preview is a blob URL fetched with the bearer token (never a token in the URL)
    await expect(img).toHaveAttribute("src", /^blob:/);
    expect(await img.evaluate((el) => (el as HTMLImageElement).naturalWidth)).toBe(8);
    expect(downloads.some((u) => /\/api\/artifacts\/[0-9a-f-]{36}\/download$/.test(u))).toBe(true);
    expect(downloads.every((u) => !u.includes("access_token"))).toBe(true);
    // only media artifacts are shown (diff and log are not)
    await expect(grid.getByTestId("media-item")).toHaveCount(1);
    await expect(page.getByRole("list", { name: "Medien-Events" })).toContainText("media.finished");

    // download button hands over the same file
    const [download] = await Promise.all([page.waitForEvent("download"), grid.getByRole("button", { name: "screenshot.png herunterladen" }).click()]);
    expect(download.suggestedFilename()).toBe("screenshot.png");

    await page.getByRole("link", { name: "Zum Job" }).click();
    await expect(page).toHaveURL(new RegExp(`/jobs/${state.jobs.main}/media$`));
  });
});

test.describe("Bugs", () => {
  test("Bug-Register mit Statusfilter", async ({ authed: page }) => {
    await page.goto("/bugs");
    const rows = page.getByTestId("bug-row");
    await expect(rows).toHaveCount(2);
    await page.getByLabel("Status").selectOption("open");
    await expect(page).toHaveURL(/\/bugs\?status=open$/);
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("BUG-UI-E2E-1");
    await expect(rows.first()).toContainText("SSE-Verbindung hinter Proxy gepuffert");
    await expect(rows.first()).toContainText("Nginx ohne X-Accel-Buffering");
    await expect(rows.first()).toContainText("P1");
    await page.getByLabel("Status").selectOption("fixed");
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText("BUG-UI-E2E-2");
    await page.getByLabel("Status").selectOption("wontfix");
    await expect(page.getByText("Keine Bugs erfasst.")).toBeVisible();
  });
});

/** Same policy as recommended for Nginx in docs/operations/ui.md. */
const CSP =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; " +
  "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'";

test.describe("Sicherheit", () => {
  test("läuft unter strikter CSP ohne Verstöße und lädt nichts von fremden Origins", async ({ authed: page, state, baseURL }) => {
    const origin = new URL(baseURL ?? "http://127.0.0.1").origin;
    const foreign: string[] = [];
    page.on("request", (r) => {
      const u = new URL(r.url());
      if ((u.protocol === "http:" || u.protocol === "https:") && u.origin !== origin) foreign.push(r.url());
    });
    await page.addInitScript(() => {
      const w = window as unknown as { __csp: string[] };
      w.__csp = [];
      document.addEventListener("securitypolicyviolation", (e) => {
        w.__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
      });
    });
    await page.route(
      (url) => !url.pathname.startsWith("/api/") && !url.pathname.startsWith("/assets/"),
      async (route) => {
        if (route.request().resourceType() !== "document") {
          await route.continue();
          return;
        }
        const response = await route.fetch();
        await route.fulfill({ response, headers: { ...response.headers(), "content-security-policy": CSP } });
      },
    );
    for (const url of ["/", `/jobs/${state.jobs.main}/plan`, `/jobs/${state.jobs.main}/changes`, `/media?job=${state.jobs.main}`]) {
      await page.goto(url);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await expect(page.locator(".loading")).toHaveCount(0);
    }
    await expect(page.getByTestId("media-grid").getByRole("img", { name: "screenshot.png" })).toHaveAttribute("src", /^blob:/);
    expect(await page.evaluate(() => (window as unknown as { __csp: string[] }).__csp)).toEqual([]);
    expect(foreign).toEqual([]);
    // positive control: the policy is really in force (an injected inline script is refused)
    const ran = await page.evaluate(async () => {
      const el = document.createElement("script");
      el.textContent = "window.__inlineRan = true";
      document.body.appendChild(el);
      await new Promise((r) => setTimeout(r, 50));
      return (window as unknown as { __inlineRan?: boolean }).__inlineRan === true;
    });
    expect(ran).toBe(false);
    await expect
      .poll(() => page.evaluate(() => (window as unknown as { __csp: string[] }).__csp.map((v) => v.split(" ")[0])))
      .toEqual(["script-src-elem"]);
  });
});
