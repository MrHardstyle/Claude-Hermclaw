import { boolLabel, formatDateTime } from "../../lib/format";
import { statusText } from "../../lib/events";
import { stepProgress } from "../../lib/plan";
import { stepStatusLabel, stepTone } from "../../lib/labels";
import { Link } from "../../lib/router";
import { Badge, KeyValue, Section } from "../../components/ui";
import type { JobTabProps } from "./types";

export function OverviewTab({ job, events }: JobTabProps) {
  const progress = stepProgress(job.steps);
  const statusLines = events.filter((e) => statusText(e) !== null).slice(-8).reverse();
  const meta = job.metadata;
  const pct = progress.total ? Math.round((progress.done / progress.total) * 100) : 0;
  return (
    <div className="tab-grid">
      <Section title="Übersicht" id="ov-main">
        <KeyValue
          items={[
            ["Job-ID", <code key="id">{job.id}</code>],
            ["Priorität", String(job.priority)],
            ["Basis-Branch", job.base_branch],
            ["Planversion", job.current_plan_version !== null ? `v${job.current_plan_version}` : null],
            ["Neuplanungen", String(job.replan_count)],
            ["Erstellt", formatDateTime(job.created_at)],
            ["Gestartet", formatDateTime(job.started_at)],
            ["Beendet", formatDateTime(job.finished_at)],
            ["Web-Research", boolLabel(typeof meta.allow_network_research === "boolean" ? meta.allow_network_research : null)],
            ["Auto-Commit", boolLabel(typeof meta.auto_commit === "boolean" ? meta.auto_commit : null)],
            ["Merge Request", typeof meta.create_merge_request === "boolean" ? boolLabel(meta.create_merge_request) : "Standard (Policy)"],
          ]}
        />
        {job.result_summary ? (
          <>
            <h3>Ergebnis</h3>
            <p className="prewrap" data-testid="job-result">
              {job.result_summary}
            </p>
          </>
        ) : null}
        {job.error_message ? (
          <p className="error-box" role="note">
            <strong>{job.error_code ?? "Fehler"}:</strong> {job.error_message} – <Link to={`/jobs/${job.id}/errors`}>Fehler ansehen</Link>
          </p>
        ) : null}
      </Section>

      <Section title="Fortschritt" id="ov-progress">
        <p>
          {progress.done} von {progress.total} Schritten abgeschlossen
          {progress.active ? `, ${progress.active} aktiv` : ""}
          {progress.failed ? `, ${progress.failed} fehlgeschlagen` : ""}.
        </p>
        <div className="progress" role="progressbar" aria-label="Schrittfortschritt" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
          <span className="progress-fill" style={{ width: `${pct}%` }} />
        </div>
        <ul className="status-chips" data-testid="step-counts">
          {Object.entries(job.step_counts).map(([k, v]) => (
            <li key={k}>
              <Badge tone={stepTone(k)}>{stepStatusLabel(k)}</Badge> <span className="chip-count">{v}</span>
            </li>
          ))}
        </ul>
        <h3>Letzte Statusmeldungen</h3>
        {statusLines.length ? (
          <ol className="status-history">
            {statusLines.map((e) => (
              <li key={e.sequence}>
                <time dateTime={e.ts}>{formatDateTime(e.ts)}</time> {statusText(e)}
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted">Noch keine Statusmeldungen.</p>
        )}
      </Section>

      <Section title="Aufgabe" id="ov-prompt" className="span-2">
        <pre className="prompt" data-testid="job-prompt">
          {job.prompt}
        </pre>
      </Section>
    </div>
  );
}
