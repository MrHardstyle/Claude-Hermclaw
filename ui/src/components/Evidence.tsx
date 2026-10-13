/** Reusable views for step evidence: tests, verifier checks, review findings, commands, tool calls. */
import type { CommandRun, ReviewRun, TestRun, ToolCall, VerificationRun } from "../api/types";
import { isFailedCheck, sortChecks, sortFindings } from "../lib/aggregate";
import { boolLabel, formatDateTime, formatDuration } from "../lib/format";
import { Link } from "../lib/router";
import { Badge, Empty, Excerpt, JsonView, ResultBadge, TableWrap } from "./ui";

const FINDING_TONE: Record<string, "danger" | "warning" | "info" | "neutral"> = { blocker: "danger", major: "warning", minor: "info" };

export function StepRef({ jobId, stepKey }: { jobId: string; stepKey: string }) {
  return (
    <Link to={`/jobs/${jobId}/steps/${encodeURIComponent(stepKey)}`} className="step-ref">
      {stepKey}
    </Link>
  );
}

export interface StepTagged<T> {
  item: T;
  stepKey?: string;
}

export function TestRunsTable({ rows, jobId }: { rows: StepTagged<TestRun>[]; jobId: string }) {
  if (!rows.length) return <Empty>Keine Testläufe.</Empty>;
  const withStep = rows.some((r) => r.stepKey);
  return (
    <TableWrap label="Testläufe">
      <table className="table" data-testid="tests-table">
        <thead>
          <tr>
            {withStep ? <th scope="col">Schritt</th> : null}
            <th scope="col">Befehl</th>
            <th scope="col">Framework</th>
            <th scope="col">Ergebnis</th>
            <th scope="col">Bestanden</th>
            <th scope="col">Fehlgeschlagen</th>
            <th scope="col">Fehler</th>
            <th scope="col">Übersprungen</th>
            <th scope="col">Dauer</th>
            <th scope="col">Zeit</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ item: t, stepKey }) => (
            <tr key={t.id} data-testid="test-row" data-status={t.status}>
              {withStep ? <td>{stepKey ? <StepRef jobId={jobId} stepKey={stepKey} /> : "–"}</td> : null}
              <td>
                <code>{t.command}</code>
                <Excerpt text={t.output_excerpt} label="Ausgabe" />
              </td>
              <td>{t.framework}</td>
              <td>
                <ResultBadge status={t.status} />
              </td>
              <td className="num">{t.passed}</td>
              <td className={`num ${t.failed ? "text-danger" : ""}`}>{t.failed}</td>
              <td className={`num ${t.errors ? "text-danger" : ""}`}>{t.errors}</td>
              <td className="num">{t.skipped}</td>
              <td>{formatDuration(t.duration_ms)}</td>
              <td>{formatDateTime(t.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableWrap>
  );
}

export function VerificationRunView({ run, stepKey, jobId }: { run: VerificationRun; stepKey?: string; jobId: string }) {
  const checks = sortChecks(run.checks);
  const failed = run.checks.filter(isFailedCheck).length;
  return (
    <article className="evidence-card" data-testid="verification-run" data-passed={String(run.passed)}>
      <header className="evidence-head">
        {stepKey ? <StepRef jobId={jobId} stepKey={stepKey} /> : null}
        <ResultBadge status={run.passed ? "pass" : "fail"} label={run.passed ? "Verifikation bestanden" : "Verifikation fehlgeschlagen"} />
        <span className="muted">
          {run.checks.length} Checks, {failed} fehlgeschlagen · {formatDateTime(run.created_at)}
        </span>
      </header>
      {run.summary ? <p>{run.summary}</p> : null}
      {run.changed_files.length ? (
        <p className="muted">
          Geänderte Dateien:{" "}
          {run.changed_files.map((f, i) => (
            <code key={i} className="tag">
              {typeof f === "string" ? f : JSON.stringify(f)}
            </code>
          ))}
        </p>
      ) : null}
      {checks.length ? (
        <TableWrap label="Verifier-Checks">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Check</th>
                <th scope="col">Typ</th>
                <th scope="col">Ergebnis</th>
                <th scope="col">Blockierend</th>
                <th scope="col">Meldung</th>
              </tr>
            </thead>
            <tbody>
              {checks.map((c) => (
                <tr key={c.id} data-testid="verifier-check" data-status={c.status} className={isFailedCheck(c) ? "row-danger" : undefined}>
                  <th scope="row">{c.name}</th>
                  <td>{c.check_type}</td>
                  <td>
                    <ResultBadge status={c.status} />
                  </td>
                  <td>{boolLabel(c.blocking)}</td>
                  <td>
                    {c.message ?? "–"}
                    {Object.keys(c.evidence).length ? <JsonView value={c.evidence} label="Evidenz" /> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      ) : (
        <Empty>Keine Checks gespeichert.</Empty>
      )}
    </article>
  );
}

export function ReviewRunView({ run, stepKey, jobId }: { run: ReviewRun; stepKey?: string; jobId: string }) {
  const findings = sortFindings(run.findings);
  return (
    <article className="evidence-card" data-testid="review-run" data-verdict={run.verdict ?? ""}>
      <header className="evidence-head">
        {stepKey ? <StepRef jobId={jobId} stepKey={stepKey} /> : null}
        <ResultBadge status={run.verdict ?? run.status} />
        <span className="muted">
          {run.model_alias ?? "Reviewer"} · {findings.length} Findings · {formatDateTime(run.created_at)}
        </span>
      </header>
      {run.invariant_override ? (
        <p className="warning-text">
          Urteil durch Invariante korrigiert: Reviewer meldete „{run.raw_verdict ?? "?"}“, aber Major/Blocker-Findings erzwingen „Korrektur nötig“.
        </p>
      ) : null}
      {run.summary ? <p>{run.summary}</p> : null}
      {findings.length ? (
        <ul className="findings">
          {findings.map((f) => (
            <li key={f.id} className={`finding sev-${f.severity}`} data-testid="review-finding" data-severity={f.severity}>
              <div className="finding-head">
                <Badge tone={FINDING_TONE[f.severity] ?? "neutral"}>{f.severity.toUpperCase()}</Badge>
                {f.path ? <code>{f.path}</code> : null}
              </div>
              <p className="finding-summary">{f.summary}</p>
              {f.evidence ? <Excerpt text={f.evidence} label="Evidenz" /> : null}
              {f.suggested_fix ? <Excerpt text={f.suggested_fix} label="Vorgeschlagene Korrektur" /> : null}
            </li>
          ))}
        </ul>
      ) : (
        <Empty>Keine Findings.</Empty>
      )}
    </article>
  );
}

export function CommandsTable({ commands }: { commands: CommandRun[] }) {
  if (!commands.length) return <Empty>Keine Befehle ausgeführt.</Empty>;
  return (
    <TableWrap label="Befehle">
      <table className="table" data-testid="commands-table">
        <thead>
          <tr>
            <th scope="col">Befehl</th>
            <th scope="col">Ziel</th>
            <th scope="col">Klasse</th>
            <th scope="col">Exit-Code</th>
            <th scope="col">Netz</th>
            <th scope="col">Dauer</th>
            <th scope="col">Zeit</th>
          </tr>
        </thead>
        <tbody>
          {commands.map((c) => (
            <tr key={c.id} className={c.exit_code !== 0 && c.exit_code !== null ? "row-danger" : undefined}>
              <td>
                <code>{c.command}</code>
                {c.cwd ? <span className="muted"> in {c.cwd}</span> : null}
                <Excerpt text={c.stdout_excerpt} label="stdout" />
                <Excerpt text={c.stderr_excerpt} label="stderr" />
              </td>
              <td>
                {c.target}
                {c.worker_id ? <span className="muted"> ({c.worker_id})</span> : null}
              </td>
              <td>{c.classification}</td>
              <td className="num">
                {c.timed_out ? <Badge tone="danger">Timeout</Badge> : (c.exit_code ?? "–")}
              </td>
              <td>{boolLabel(c.network)}</td>
              <td>{formatDuration(c.duration_ms)}</td>
              <td>{formatDateTime(c.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableWrap>
  );
}

export function ToolCallsTable({ calls }: { calls: ToolCall[] }) {
  if (!calls.length) return <Empty>Keine Tool-Aufrufe.</Empty>;
  return (
    <TableWrap label="Tool-Aufrufe">
      <table className="table" data-testid="toolcalls-table">
        <thead>
          <tr>
            <th scope="col">Runde</th>
            <th scope="col">Tool</th>
            <th scope="col">Ergebnis</th>
            <th scope="col">Zusammenfassung</th>
            <th scope="col">Dauer</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((t) => (
            <tr key={t.id}>
              <td className="num">{t.turn}</td>
              <td>
                <code>{t.tool}</code>
              </td>
              <td>
                <ResultBadge status={t.status} />
                {t.error_code ? <code className="muted"> {t.error_code}</code> : null}
              </td>
              <td>
                {t.result_summary ?? "–"}
                {Object.keys(t.arguments).length ? <JsonView value={t.arguments} label="Argumente" /> : null}
              </td>
              <td>{formatDuration(t.duration_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableWrap>
  );
}
