import { useId, useState } from "react";
import { EventList } from "../../components/EventList";
import { Badge, ErrorBox, Loading, Section } from "../../components/ui";
import { CATEGORY_LABELS, eventCategory, isErrorEvent, statusText } from "../../lib/events";
import { formatDateTime, formatDuration, formatTime } from "../../lib/format";
import { STREAM_STATE_LABELS, type StreamInfo } from "../../lib/useEvents";
import type { JobTabProps } from "./types";

export function StreamBadge({ stream }: { stream: StreamInfo }) {
  const tone = stream.state === "open" ? "success" : stream.state === "reconnecting" ? "warning" : "neutral";
  const retry = stream.nextRetryMs !== null ? ` (in ${formatDuration(stream.nextRetryMs)}, Versuch ${String(stream.attempt)})` : "";
  return (
    <Badge tone={tone} testId="stream-state" title={stream.state}>
      {STREAM_STATE_LABELS[stream.state]}
      {retry}
    </Badge>
  );
}

export function EventsTab({ events, eventsState, stepKeys }: JobTabProps) {
  const [category, setCategory] = useState("");
  const [type, setType] = useState("");
  const [query, setQuery] = useState("");
  const [onlyProblems, setOnlyProblems] = useState(false);
  const ids = { cat: useId(), type: useId(), q: useId(), prob: useId() };

  const categories = [...new Set(events.map((e) => eventCategory(e.event_type)))].sort();
  const types = [...new Set(events.filter((e) => !category || eventCategory(e.event_type) === category).map((e) => e.event_type))].sort();
  const q = query.trim().toLowerCase();
  const filtered = events.filter((e) => {
    if (category && eventCategory(e.event_type) !== category) return false;
    if (type && e.event_type !== type) return false;
    if (onlyProblems && !isErrorEvent(e)) return false;
    if (q && !`${e.event_type} ${JSON.stringify(e.payload)}`.toLowerCase().includes(q)) return false;
    return true;
  });
  const statusLines = events.filter((e) => statusText(e) !== null);
  const latest = statusLines[statusLines.length - 1];

  return (
    <div className="stack">
      <Section title="Live-Status" id="ev-status" actions={<StreamBadge stream={eventsState.stream} />}>
        {latest ? (
          <p className="status-now" data-testid="events-status-now">
            <time dateTime={latest.ts}>{formatTime(latest.ts)}</time> {statusText(latest)}
          </p>
        ) : (
          <p className="muted">Noch keine Statusmeldung.</p>
        )}
        {statusLines.length > 1 ? (
          <ol className="status-history" aria-label="Statusverlauf" data-testid="status-history">
            {statusLines
              .slice(-12, -1)
              .reverse()
              .map((e) => (
                <li key={e.sequence}>
                  <time dateTime={e.ts}>{formatDateTime(e.ts)}</time> {statusText(e)}
                </li>
              ))}
          </ol>
        ) : null}
      </Section>

      <Section title={`Events (${String(filtered.length)} von ${String(events.length)})`} id="ev-list">
        <div className="filters" role="search" aria-label="Events filtern">
          <div className="inline-field">
            <label htmlFor={ids.cat}>Bereich</label>
            <select
              id={ids.cat}
              value={category}
              onChange={(e) => {
                setCategory(e.target.value);
                setType("");
              }}
              data-testid="event-category-filter"
            >
              <option value="">Alle</option>
              {categories.map((c) => (
                <option key={c} value={c}>
                  {CATEGORY_LABELS[c] ?? c}
                </option>
              ))}
            </select>
          </div>
          <div className="inline-field">
            <label htmlFor={ids.type}>Typ</label>
            <select id={ids.type} value={type} onChange={(e) => setType(e.target.value)} data-testid="event-type-filter">
              <option value="">Alle</option>
              {types.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div className="inline-field">
            <label htmlFor={ids.q}>Suche</label>
            <input id={ids.q} type="search" value={query} onChange={(e) => setQuery(e.target.value)} data-testid="event-search" />
          </div>
          <label className="check" htmlFor={ids.prob}>
            <input id={ids.prob} type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} /> Nur Warnungen/Fehler
          </label>
        </div>
        {eventsState.error ? <ErrorBox error={eventsState.error} /> : null}
        {!eventsState.loaded ? <Loading label="Events werden geladen …" /> : null}
        <EventList events={filtered} stepKeys={stepKeys} label="Job-Events (neueste zuerst)" />
      </Section>
    </div>
  );
}
