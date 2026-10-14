import { useState } from "react";
import { api, describeError } from "../../api/client";
import type { SshApprovalPending } from "../../api/types";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { AsyncBlock, Badge, Empty, Section, TableWrap } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { useApi } from "../../lib/useApi";
import type { JobTabProps } from "./types";

/**
 * Operator approvals of mutating SSH commands (P26): a step that wants to change a remote host stops with
 * "approval_required"; approving the exact host + command lets the next attempt run it once. Needs the admin scope.
 */
export function ApprovalsTab({ job, events, stepKeys }: JobTabProps) {
  const version = events.filter((e) => e.event_type.startsWith("ssh.")).length;
  const approvals = useApi(`ssh-approvals:${job.id}`, (s) => api.sshApprovals(job.id, s), { version });
  const [pending, setPending] = useState<SshApprovalPending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setPending(null);
      approvals.reload();
    } catch (exc) {
      setError(describeError(exc));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <AsyncBlock result={approvals} loadingLabel="Lädt Freigaben …">
        {(data) => (
          <>
            <Section title="Offene SSH-Freigaben" id="ssh-pending">
              {data.pending.length ? (
                <TableWrap label="Offene SSH-Freigaben">
                  <table data-testid="ssh-pending">
                    <thead>
                      <tr>
                        <th scope="col">Host</th>
                        <th scope="col">Befehl</th>
                        <th scope="col">Schritt</th>
                        <th scope="col">Angefragt</th>
                        <th scope="col">Aktion</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.pending.map((p) => (
                        <tr key={p.token}>
                          <td>{p.host ?? "–"}</td>
                          <td>
                            <code>{p.command ?? ""}</code> {p.classification ? <Badge tone="warning">{p.classification}</Badge> : null}
                          </td>
                          <td>{p.step_id ? (stepKeys.get(p.step_id) ?? p.step_id.slice(0, 8)) : "–"}</td>
                          <td>{p.requested_at ? formatDateTime(p.requested_at) : "–"}</td>
                          <td>
                            <button type="button" className="btn btn-small" onClick={() => setPending(p)} data-testid="ssh-approve">
                              Freigeben …
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableWrap>
              ) : (
                <Empty>Keine SSH-Befehle warten auf Freigabe.</Empty>
              )}
            </Section>
            <Section title="Erteilte Freigaben" id="ssh-approved">
              {data.approved.length || data.consumed.length ? (
                <ul className="mini-list" data-testid="ssh-approved">
                  {data.approved.map((a) => (
                    <li key={a.token}>
                      <Badge tone="success">bereit</Badge> <code>{a.token.slice(0, 16)}…</code> {a.approved_by ? `von ${a.approved_by}` : ""}{" "}
                      {a.note ? <span className="muted">– {a.note}</span> : null}{" "}
                      <button type="button" className="btn btn-small" disabled={busy} onClick={() => void act(() => api.revokeSsh(job.id, a.token))}>
                        Widerrufen
                      </button>
                    </li>
                  ))}
                  {data.consumed.map((a, i) => (
                    <li key={`${a.token}-${String(i)}`}>
                      <Badge tone="neutral">verbraucht</Badge> <code>{a.token.slice(0, 16)}…</code> {a.approved_by ? `von ${a.approved_by}` : ""}
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>Noch keine Freigaben.</Empty>
              )}
              {error && !pending ? <p className="error-text">{error}</p> : null}
            </Section>
          </>
        )}
      </AsyncBlock>
      <ConfirmDialog
        open={pending !== null}
        title="SSH-Befehl freigeben?"
        message={pending ? `Host ${pending.host ?? "?"}: ${pending.command ?? ""} – wird beim nächsten Versuch des Schritts genau einmal ausgeführt.` : ""}
        confirmLabel="Freigeben"
        askReason
        busy={busy}
        error={error}
        onConfirm={(note) => {
          if (pending) void act(() => api.approveSsh(job.id, pending.token, note));
        }}
        onCancel={() => {
          setPending(null);
          setError(null);
        }}
      />
    </div>
  );
}
