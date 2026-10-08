import type { ReactNode } from "react";
import { describeError } from "../api/client";
import type { Json } from "../api/types";
import {
  jobStatusLabel,
  jobTone,
  resultTone,
  severityLabel,
  severityTone,
  stepStatusLabel,
  stepTone,
  workerTone,
  WORKER_STATE_HINTS,
  type Tone,
} from "../lib/labels";

export function Badge({ tone, children, title, testId }: { tone: Tone; children: ReactNode; title?: string; testId?: string }) {
  return (
    <span className={`badge tone-${tone}`} title={title} data-testid={testId}>
      {children}
    </span>
  );
}

export function JobStatusBadge({ status }: { status: string }) {
  return (
    <Badge tone={jobTone(status)} title={status} testId="job-status">
      {jobStatusLabel(status)}
    </Badge>
  );
}

export function StepStatusBadge({ status }: { status: string }) {
  return (
    <Badge tone={stepTone(status)} title={status}>
      {stepStatusLabel(status)}
    </Badge>
  );
}

export function WorkerStateBadge({ state }: { state: string }) {
  const s = state.toUpperCase();
  return (
    <Badge tone={workerTone(s)} title={WORKER_STATE_HINTS[s] ?? s} testId="worker-state">
      {s}
    </Badge>
  );
}

export function SeverityBadge({ severity }: { severity: string }) {
  return <Badge tone={severityTone(severity)}>{severityLabel(severity)}</Badge>;
}

export function ResultBadge({ status, label }: { status: string; label?: string }) {
  return <Badge tone={resultTone(status)}>{label ?? status}</Badge>;
}

export function Section({
  title,
  children,
  actions,
  id,
  className,
}: {
  title: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
  id?: string;
  className?: string;
}) {
  const headingId = id ? `${id}-heading` : undefined;
  return (
    <section className={`card ${className ?? ""}`} aria-labelledby={headingId} id={id}>
      <div className="card-head">
        <h2 id={headingId}>{title}</h2>
        {actions ? <div className="card-actions">{actions}</div> : null}
      </div>
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Loading({ label = "Lädt …" }: { label?: string }) {
  return (
    <p className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" /> {label}
    </p>
  );
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="error-box" role="alert">
      <strong>Fehler:</strong> {describeError(error)}
      {onRetry ? (
        <button type="button" className="btn btn-small" onClick={onRetry}>
          Erneut laden
        </button>
      ) : null}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

/** Shared "loading / error / content" switch for useApi results. */
export function AsyncBlock<T>({
  result,
  children,
  loadingLabel,
}: {
  result: { data: T | undefined; error: unknown; loading: boolean; reload: () => void };
  children: (data: T) => ReactNode;
  loadingLabel?: string;
}) {
  if (result.data !== undefined) {
    return (
      <>
        {result.error ? <ErrorBox error={result.error} onRetry={result.reload} /> : null}
        {children(result.data)}
      </>
    );
  }
  if (result.error) return <ErrorBox error={result.error} onRetry={result.reload} />;
  return <Loading label={loadingLabel} />;
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v]) => (
        <div key={k} className="kv-row">
          <dt>{k}</dt>
          <dd>{v === null || v === undefined || v === "" ? "–" : v}</dd>
        </div>
      ))}
    </dl>
  );
}

export function JsonView({ value, label = "Details (JSON)" }: { value: Json | Record<string, unknown> | unknown[]; label?: string }) {
  return (
    <details className="json-view">
      <summary>{label}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

export function Excerpt({ text, label }: { text: string | null | undefined; label: string }) {
  if (!text) return null;
  return (
    <details className="excerpt">
      <summary>{label}</summary>
      <pre>{text}</pre>
    </details>
  );
}

export function TableWrap({ children, label }: { children: ReactNode; label: string }) {
  return (
    // scroll container must be focusable so keyboard users can scroll wide tables
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
    <div className="table-wrap" tabIndex={0} role="region" aria-label={label}>
      {children}
    </div>
  );
}

export function Meter({ value, label }: { value: number; label: string }) {
  const v = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0));
  const pct = Math.round(v * 100);
  return (
    <span className="meter" title={`${label}: ${pct} %`}>
      <span className="meter-track" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <span className="meter-fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="meter-value">{(v).toFixed(2).replace(".", ",")}</span>
    </span>
  );
}

export function Stat({ label, value, tone, testId }: { label: string; value: ReactNode; tone?: Tone; testId?: string }) {
  return (
    <div className={`stat ${tone ? `tone-${tone}` : ""}`} data-testid={testId}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}
