import { apiGet, emitEvent, expect, network, test } from "./fixtures";

test.describe("Live-Events (31.5)", () => {
  test("neue Events kommen live per SSE, Statuszeilen prominent, Filter nach Typ", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.main}/events`);
    await expect(page.getByTestId("stream-state").first()).toHaveText("Live verbunden");
    const rows = page.getByTestId("event-row");
    const before = await rows.count();
    expect(before).toBeGreaterThan(20);
    // seeded status lines are visible
    await expect(page.getByTestId("status-history")).toContainText("Gemma erstellt Plan");
    await expect(page.getByTestId("status-history")).toContainText("Coder bearbeitet src/api.py");

    const text = `Coder bearbeitet src/live_${String(Date.now())}.py`;
    const seq = emitEvent(state.jobs.main, text);
    // header status line and the "now" line update without reload
    await expect(page.getByTestId("status-line")).toContainText(text);
    await expect(page.getByTestId("events-status-now")).toContainText(text);
    const newRow = page.locator(`[data-testid="event-row"][data-seq="${String(seq)}"]`);
    await expect(newRow).toHaveClass(/is-status/);
    await expect(rows.first()).toHaveAttribute("data-seq", String(seq)); // newest first
    await expect(rows).toHaveCount(before + 1);

    // an event type the stream has no listener for still arrives (REST reconciliation)
    const custom = `Benutzerdefiniert ${String(Date.now())}`;
    const seq2 = emitEvent(state.jobs.main, custom, "custom.unknown");
    await expect(page.locator(`[data-testid="event-row"][data-seq="${String(seq2)}"]`)).toContainText(custom, { timeout: 25_000 });

    // filter by type
    await page.getByTestId("event-type-filter").selectOption("status");
    for (const r of await rows.all()) await expect(r).toHaveAttribute("data-type", "status");
    await page.getByTestId("event-type-filter").selectOption("");
    const all = await apiGet<{ event_type: string }[]>(`/jobs/${state.jobs.main}/events?limit=5000`);
    const gitCount = all.filter((e) => e.event_type.startsWith("git.")).length;
    expect(gitCount).toBeGreaterThan(0);
    await page.getByTestId("event-category-filter").selectOption("git");
    await expect(rows).toHaveCount(gitCount);
    for (const r of await rows.all()) await expect(r).toHaveAttribute("data-type", /^git\./);
    await page.getByTestId("event-category-filter").selectOption("");
    await page.getByTestId("event-search").fill("LiteLLM");
    await expect(rows).toHaveCount(1);
  });

  test("nach Verbindungsabbruch wird neu verbunden und verpasste Events werden per Last-Event-ID nachgeliefert", async ({ authed: page, state }) => {
    const streamRequests: { url: string; lastEventId: string | undefined }[] = [];
    page.on("request", (req) => {
      if (req.url().includes("/events/stream")) streamRequests.push({ url: req.url(), lastEventId: req.headers()["last-event-id"] });
    });
    await page.goto(`/jobs/${state.jobs.main}/events`);
    const badge = page.getByTestId("stream-state").first();
    await expect(badge).toHaveText("Live verbunden");
    const beforeSeq = Number(await page.getByTestId("event-row").first().getAttribute("data-seq"));
    expect(beforeSeq).toBeGreaterThan(0);

    const missed = `Während offline ${String(Date.now())}`;
    let seq: number | undefined;
    try {
      // real connection loss between browser and UI server; new connections are refused while blocked
      await network("ui", "block");
      await expect(badge).toContainText("Verbindung unterbrochen", { timeout: 20_000 });
      seq = emitEvent(state.jobs.main, missed);
      await expect(page.locator(`[data-testid="event-row"][data-seq="${String(seq)}"]`)).toHaveCount(0);
    } finally {
      await network("ui", "unblock");
    }
    expect(seq).toBeGreaterThan(beforeSeq);
    await expect(badge).toHaveText("Live verbunden", { timeout: 40_000 });
    await expect(page.locator(`[data-testid="event-row"][data-seq="${String(seq)}"]`)).toContainText(missed, { timeout: 20_000 });
    // the reconnect carried the cursor (Last-Event-ID header from the browser or ?last_event_id= from the backoff loop)
    const cursorOf = (r: { url: string; lastEventId: string | undefined }) =>
      Math.max(Number(new URL(r.url).searchParams.get("last_event_id") ?? "0"), Number(r.lastEventId ?? "0"));
    expect(
      streamRequests.slice(1).some((r) => cursorOf(r) >= beforeSeq),
      JSON.stringify(streamRequests),
    ).toBe(true);
    // the token travels as query parameter (EventSource cannot set headers)
    expect(streamRequests.every((r) => new URL(r.url).searchParams.get("access_token") === state.token)).toBe(true);

    // and live delivery works again afterwards
    const after = `Nach Reconnect ${String(Date.now())}`;
    emitEvent(state.jobs.main, after);
    await expect(page.getByTestId("events-status-now")).toContainText(after, { timeout: 5_000 });
  });

  test("halb offene Verbindung (Upstream hinter dem Proxy tot) wird erkannt und ersetzt", async ({ authed: page, state }) => {
    test.slow(); // waits for one REST reconciliation round (15 s) plus the stale grace period (5 s)
    let streamRequests = 0;
    page.on("request", (req) => {
      if (req.url().includes(`/jobs/${state.jobs.main}/events/stream`)) streamRequests += 1;
    });
    await page.goto(`/jobs/${state.jobs.main}/events`);
    const badge = page.getByTestId("stream-state").first();
    await expect(badge).toHaveText("Live verbunden");
    expect(streamRequests).toBe(1);

    // kill only the proxy → API connections: the browser side stays open but silent
    const dropped = await network("api", "drop");
    expect(dropped.dropped).toBeGreaterThan(0); // the SSE upstream (and idle keep-alive sockets) were cut
    const silent = `Hinter totem Upstream ${String(Date.now())}`;
    const seq = emitEvent(state.jobs.main, silent);
    // REST reconciliation still finds it …
    await expect(page.locator(`[data-testid="event-row"][data-seq="${String(seq)}"]`)).toContainText(silent, { timeout: 25_000 });
    // … and the watchdog replaces the dead stream
    await expect.poll(() => streamRequests, { timeout: 15_000 }).toBeGreaterThan(1);
    await expect(badge).toHaveText("Live verbunden", { timeout: 15_000 });
    // live again: a new event arrives faster than the next reconciliation round could deliver it
    const live = `Wieder live ${String(Date.now())}`;
    emitEvent(state.jobs.main, live);
    await expect(page.getByTestId("events-status-now")).toContainText(live, { timeout: 5_000 });
  });
});
