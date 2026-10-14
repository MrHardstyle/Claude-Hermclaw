import { expect, test } from "./fixtures";

test.describe("SSH-Freigaben (P26)", () => {
  test("offener SSH-Befehl wird nach Bestätigung freigegeben und kann widerrufen werden", async ({ authed: page, state }) => {
    await page.goto(`/jobs/${state.jobs.main}/approvals`);
    const pending = page.getByTestId("ssh-pending");
    await expect(pending).toContainText("webui-223");
    await expect(pending).toContainText("sudo /usr/bin/systemctl reload nginx");
    await expect(pending).toContainText("S3");

    await pending.getByTestId("ssh-approve").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("genau einmal");
    await dialog.getByRole("textbox").fill("Konfiguration geprüft");
    await dialog.getByTestId("confirm-action").click();

    await expect(page.getByText("Keine SSH-Befehle warten auf Freigabe.")).toBeVisible();
    const approved = page.getByTestId("ssh-approved");
    await expect(approved).toContainText("bereit");
    await expect(approved).toContainText("Konfiguration geprüft");
    await expect(approved).toContainText(state.ssh_approval.slice(0, 16));

    // server state: the token is approved
    const res = await page.request.get(`${state.api_url}/api/jobs/${state.jobs.main}/ssh-approvals`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    expect(((await res.json()) as { approved: { token: string }[] }).approved.map((a) => a.token)).toContain(state.ssh_approval);

    await approved.getByRole("button", { name: "Widerrufen" }).click();
    await expect(page.getByTestId("ssh-pending")).toContainText("systemctl reload nginx");
  });
});
