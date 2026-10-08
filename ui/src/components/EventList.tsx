import type { HermEvent } from "../api/types";
import { CATEGORY_LABELS, eventCategory, statusText, summarizeEvent } from "../lib/events";
import { formatDateTime, formatTime } from "../lib/format";
import { SeverityBadge } from "./ui";

export function EventRow({ ev, stepKey }: { ev: HermEvent; stepKey?: string | null }) {
  const status = statusText(ev);
  const cat = eventCategory(ev.event_type);
  return (
    <li className={`event-row sev-${ev.severity} ${status ? "is-status" : ""}`} data-testid="event-row" data-type={ev.event_type} data-seq={ev.sequence}>
      <time dateTime={ev.ts} title={formatDateTime(ev.ts)} className="event-time">
        {formatTime(ev.ts)}
      </time>
      <span className="event-type" title={ev.event_type}>
        <span className="event-cat">{CATEGORY_LABELS[cat] ?? cat}</span> <code>{ev.event_type}</code>
      </span>
      {ev.severity !== "info" ? <SeverityBadge severity={ev.severity} /> : null}
      {stepKey ? <span className="event-step">{stepKey}</span> : null}
      <span className={status ? "event-status-text" : "event-summary"}>{summarizeEvent(ev)}</span>
      {!status && Object.keys(ev.payload).length ? (
        <details className="event-payload">
          <summary>Payload</summary>
          <pre>{JSON.stringify(ev.payload, null, 2)}</pre>
        </details>
      ) : null}
    </li>
  );
}

export function EventList({
  events,
  stepKeys,
  newestFirst = true,
  limit = 500,
  label,
}: {
  events: readonly HermEvent[];
  stepKeys?: Map<string, string>;
  newestFirst?: boolean;
  limit?: number;
  label: string;
}) {
  const list = newestFirst ? [...events].reverse() : [...events];
  const shown = list.slice(0, limit);
  return (
    <>
      <ol className="event-list" aria-label={label}>
        {shown.map((ev) => (
          <EventRow key={ev.sequence} ev={ev} stepKey={ev.step_id ? (stepKeys?.get(ev.step_id) ?? null) : null} />
        ))}
      </ol>
      {list.length > shown.length ? (
        <p className="muted">
          {shown.length} von {list.length} Events angezeigt.
        </p>
      ) : null}
    </>
  );
}
