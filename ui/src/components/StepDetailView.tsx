import { api } from "../api/client";
import type { Json, Step, StepDetail } from "../api/types";
import { describeAcceptance } from "../lib/aggregate";
import { formatDateTime, formatNumber } from "../lib/format";
import { KIND_LABELS } from "../lib/labels";
import { useApi } from "../lib/useApi";
import { CommandsTable, ReviewRunView, TestRunsTable, ToolCallsTable, VerificationRunView } from "./Evidence";
import { AsyncBlock, Badge, Empty, JsonView, KeyValue, ResultBadge, StepStatusBadge, TableWrap } from "./ui";

function strList(v: Json | undefined): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

function PathList({ label, paths }: { label: string; paths: string[] }) {
  return (
    <div className="path-list">
      <span className="muted">{label}:</span>{" "}
      {paths.length ? (
        paths.map((p) => (
          <code key={p} className="tag">
            {p}
          </code>
        ))
      ) : (
        <span className="muted">–</span>
      )}
    </div>
  );
}

function StepDetailBody({ d, jobId }: { d: StepDetail; jobId: string }) {
  const s = d.step;
  return (
    <div className="step-detail" data-testid="step-detail" data-step={s.step_key}>
      <h3 className="step-detail-title">
        {s.step_key} – {s.title} <StepStatusBadge status={s.status} />
      </h3>
      <KeyValue
        items={[
          ["Art", KIND_LABELS[s.kind] ?? s.kind],
          ["Fähigkeit", s.capability],
          ["Risiko", s.risk],
          ["Abhängig von", s.depends_on.length ? s.depends_on.join(", ") : "–"],
          ["Versuche", formatNumber(s.attempt_count)],
          ["Korrekturen", formatNumber(s.correction_count)],
          ["Worker", s.assigned_worker_id],
          ["Scope-Version", s.current_scope_version !== null ? `v${s.current_scope_version}` : null],
          ["Gestartet", formatDateTime(s.started_at)],
          ["Beendet", formatDateTime(s.finished_at)],
        ]}
      />
      <h4>Ziel</h4>
      <p className="prewrap">{s.goal}</p>
      {s.error_message ? (
        <p className="error-box" role="note">
          <strong>{s.error_code ?? "Fehler"}:</strong> {s.error_message}
        </p>
      ) : null}
      <h4>Akzeptanzkriterien</h4>
      {s.acceptance.length ? (
        <ul className="acceptance">
          {s.acceptance.map((c, i) => {
            const a = describeAcceptance(c);
            return (
              <li key={i}>
                <Badge tone="neutral">{a.type}</Badge> {a.text}
              </li>
            );
          })}
        </ul>
      ) : (
        <Empty>Keine Akzeptanzkriterien.</Empty>
      )}

      <h4>Versuche ({d.attempts.length})</h4>
      {d.attempts.length ? (
        <TableWrap label="Versuche">
          <table className="table" data-testid="attempts-table">
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Art</th>
                <th scope="col">Status</th>
                <th scope="col">Ergebnis</th>
                <th scope="col">Worker</th>
                <th scope="col">Modell</th>
                <th scope="col">Runden</th>
                <th scope="col">Zusammenfassung</th>
                <th scope="col">Gestartet</th>
                <th scope="col">Beendet</th>
              </tr>
            </thead>
            <tbody>
              {d.attempts.map((a) => (
                <tr key={a.id} data-testid="attempt-row">
                  <td className="num">{a.attempt_no}</td>
                  <td>{a.kind}</td>
                  <td>
                    <ResultBadge status={a.status} />
                  </td>
                  <td>
                    {a.outcome ?? "–"}
                    {a.error_code ? <code className="muted"> {a.error_code}</code> : null}
                  </td>
                  <td>{a.worker_id ?? "–"}</td>
                  <td>{a.model_alias ?? "–"}</td>
                  <td className="num">{a.turns_used}</td>
                  <td>{a.summary ?? "–"}</td>
                  <td>{formatDateTime(a.started_at)}</td>
                  <td>{formatDateTime(a.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      ) : (
        <Empty>Noch keine Versuche.</Empty>
      )}

      <h4>Scope-Versionen ({d.scope_versions.length})</h4>
      {d.scope_versions.length ? (
        <ol className="scope-versions" data-testid="scope-versions">
          {d.scope_versions.map((sv) => (
            <li key={sv.id} className="evidence-card">
              <div className="evidence-head">
                <strong>v{sv.version}</strong> <ResultBadge status={sv.status} />{" "}
                <span className="muted">
                  {typeof sv.contract.source === "string" ? sv.contract.source : ""} · {formatDateTime(sv.created_at)}
                </span>
              </div>
              {sv.reason ? <p>{sv.reason}</p> : null}
              <PathList label="Zieldateien" paths={strList(sv.contract.target_paths)} />
              <PathList label="Neue Pfade erlaubt" paths={strList(sv.contract.allowed_new_paths)} />
              <PathList label="Verboten" paths={strList(sv.contract.forbidden_paths)} />
              <PathList label="Operationen" paths={strList(sv.contract.allowed_operations)} />
              <JsonView value={sv.contract} label="Vertrag (JSON)" />
              {Object.keys(sv.evidence).length ? <JsonView value={sv.evidence} label="Evidenz (JSON)" /> : null}
            </li>
          ))}
        </ol>
      ) : (
        <Empty>Kein Scope-Vertrag gespeichert.</Empty>
      )}

      <h4>Tool-Aufrufe ({d.tool_calls.length})</h4>
      <ToolCallsTable calls={d.tool_calls} />

      <h4>Befehle ({d.commands.length})</h4>
      <CommandsTable commands={d.commands} />

      <h4>Tests ({d.tests.length})</h4>
      <TestRunsTable rows={d.tests.map((t) => ({ item: t }))} jobId={jobId} />

      <h4>Verifier ({d.verifications.length})</h4>
      {d.verifications.length ? (
        d.verifications.map((v) => <VerificationRunView key={v.id} run={v} jobId={jobId} />)
      ) : (
        <Empty>Noch keine Verifikation.</Empty>
      )}

      <h4>Review ({d.reviews.length})</h4>
      {d.reviews.length ? d.reviews.map((r) => <ReviewRunView key={r.id} run={r} jobId={jobId} />) : <Empty>Noch kein Review.</Empty>}

      {Object.keys(s.result).length ? <JsonView value={s.result} label="Ergebnis (JSON)" /> : null}
    </div>
  );
}

/** Loads `GET /api/steps/{id}`; `version` (step-level event count) refreshes it live. */
export function StepDetailView({ step, jobId, version }: { step: Step; jobId: string; version: number }) {
  const detail = useApi(`step:${step.id}`, (s) => api.step(step.id, s), { version });
  return <AsyncBlock result={detail}>{(d) => <StepDetailBody d={d} jobId={jobId} />}</AsyncBlock>;
}
