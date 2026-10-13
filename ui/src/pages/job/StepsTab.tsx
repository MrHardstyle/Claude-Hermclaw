import { useId, useState } from "react";
import { StepDetailView } from "../../components/StepDetailView";
import { Empty, Section, StepStatusBadge, TableWrap } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { KIND_LABELS } from "../../lib/labels";
import { Link } from "../../lib/router";
import type { JobTabProps } from "./types";

export function StepsTab({ job, sub, events }: JobTabProps & { sub: string | null }) {
  const [showSuperseded, setShowSuperseded] = useState(false);
  const toggleId = useId();
  const steps = job.steps.filter((s) => showSuperseded || !s.superseded);
  const selected =
    sub === null
      ? null
      : (job.steps.find((s) => s.step_key === sub && !s.superseded) ?? job.steps.find((s) => s.step_key === sub || s.id === sub) ?? null);
  // per-step refresh trigger: events of the selected step
  const version = selected ? events.filter((e) => e.step_id === selected.id).length : 0;

  return (
    <div className="stack">
      <Section
        title="Schritte"
        id="steps-list"
        actions={
          <label className="check" htmlFor={toggleId}>
            <input id={toggleId} type="checkbox" checked={showSuperseded} onChange={(e) => setShowSuperseded(e.target.checked)} /> Ersetzte Schritte
            anzeigen
          </label>
        }
      >
        {steps.length ? (
          <TableWrap label="Schritte">
            <table className="table" data-testid="steps-table">
              <thead>
                <tr>
                  <th scope="col">Schritt</th>
                  <th scope="col">Titel</th>
                  <th scope="col">Art</th>
                  <th scope="col">Status</th>
                  <th scope="col">Abhängig von</th>
                  <th scope="col">Versuche</th>
                  <th scope="col">Korrekturen</th>
                  <th scope="col">Worker</th>
                  <th scope="col">Beendet</th>
                </tr>
              </thead>
              <tbody>
                {steps.map((s) => (
                  <tr
                    key={s.id}
                    className={`${s.id === selected?.id ? "row-selected" : ""} ${s.superseded ? "row-muted" : ""}`}
                    data-testid="step-row"
                    data-step={s.step_key}
                  >
                    <th scope="row">
                      <Link to={`/jobs/${job.id}/steps/${encodeURIComponent(s.step_key)}`} aria-current={s.id === selected?.id ? "true" : undefined}>
                        {s.step_key}
                      </Link>
                      {s.superseded ? <span className="muted"> (ersetzt)</span> : null}
                    </th>
                    <td>{s.title}</td>
                    <td>{KIND_LABELS[s.kind] ?? s.kind}</td>
                    <td>
                      <StepStatusBadge status={s.status} />
                    </td>
                    <td>{s.depends_on.join(", ") || "–"}</td>
                    <td className="num">{s.attempt_count}</td>
                    <td className="num">{s.correction_count}</td>
                    <td>{s.assigned_worker_id ?? "–"}</td>
                    <td>{formatDateTime(s.finished_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        ) : (
          <Empty>Noch keine Schritte – der Plan wird erstellt.</Empty>
        )}
      </Section>
      <Section title={selected ? `Schritt ${selected.step_key}` : "Schrittdetails"} id="step-detail">
        {selected ? (
          <StepDetailView key={selected.id} step={selected} jobId={job.id} version={version + selected.attempt_count} />
        ) : sub ? (
          <Empty>Schritt „{sub}“ nicht gefunden.</Empty>
        ) : (
          <Empty>Einen Schritt auswählen, um Versuche, Scope, Tool-Aufrufe, Befehle, Tests, Verifier und Review zu sehen.</Empty>
        )}
      </Section>
    </div>
  );
}
