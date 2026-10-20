import { useId, useState, type SyntheticEvent } from "react";
import { api, ApiError, describeError } from "../api/client";
import type { MemoryEntry, MemorySearchHit, MemoryStatus } from "../api/types";
import { AsyncBlock, Badge, Empty, Excerpt, JsonView, Section, TableWrap } from "../components/ui";
import { formatDateTime, shortId } from "../lib/format";
import {
  buildMemoryBody,
  EMPTY_MEMORY_FORM,
  MEMORY_KINDS,
  MEMORY_LIMITS,
  MEMORY_SOURCES,
  MEMORY_STATUSES,
  memoryFormFrom,
  memoryStatusLabel,
  memoryStatusTone,
  type MemoryFormErrors,
  type MemoryFormValues,
} from "../lib/memoryForm";
import { Link, useRouter } from "../lib/router";
import { useApi } from "../lib/useApi";

function FieldError({ id, error }: { id: string; error: string | undefined }) {
  if (!error) return null;
  return (
    <small id={id} className="field-error" role="alert">
      {error}
    </small>
  );
}

/** New entry (entry = null) or edit of an existing one. */
function MemoryForm({ entry, onSaved, onCancel }: { entry: MemoryEntry | null; onSaved: (e: MemoryEntry) => void; onCancel: () => void }) {
  const [values, setValues] = useState<MemoryFormValues>(() => (entry ? memoryFormFrom(entry) : EMPTY_MEMORY_FORM));
  const [errors, setErrors] = useState<MemoryFormErrors>({});
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const uid = useId();
  const id = (name: string) => `${uid}-${name}`;

  const set = <K extends keyof MemoryFormValues>(key: K, value: MemoryFormValues[K]) => {
    setValues((v) => ({ ...v, [key]: value }));
    if (errors[key]) setErrors((e) => ({ ...e, [key]: undefined }));
  };

  const submit = async (e: SyntheticEvent) => {
    e.preventDefault();
    setServerError(null);
    const { body, errors: errs } = buildMemoryBody(values);
    setErrors(errs);
    if (!body) return;
    setBusy(true);
    try {
      onSaved(entry ? await api.updateMemory(entry.id, body) : await api.createMemory(body));
    } catch (err) {
      setServerError(err instanceof ApiError && err.status === 422 ? `Eingaben ungültig: ${err.message}` : describeError(err));
    } finally {
      setBusy(false);
    }
  };

  const described = (name: keyof MemoryFormValues, hint?: string) => {
    const ids = [hint, errors[name] ? id(`${name}-err`) : undefined].filter(Boolean).join(" ");
    return { "aria-describedby": ids || undefined, "aria-invalid": errors[name] ? true : undefined };
  };

  return (
    <form
      className="job-form"
      onSubmit={(e) => void submit(e)}
      noValidate
      aria-label={entry ? "Wissenseintrag bearbeiten" : "Neuen Wissenseintrag anlegen"}
      data-testid="memory-form"
    >
      <div className="field-row">
        <div className="field">
          <label htmlFor={id("kind")}>Art</label>
          <select id={id("kind")} value={values.kind} onChange={(e) => set("kind", e.target.value as MemoryFormValues["kind"])}>
            {MEMORY_KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={id("status")}>Status</label>
          <select id={id("status")} value={values.status} onChange={(e) => set("status", e.target.value as MemoryStatus)}>
            {MEMORY_STATUSES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={id("scope")}>Gilt für</label>
          <input
            id={id("scope")}
            type="text"
            placeholder="überall"
            value={values.scope}
            maxLength={MEMORY_LIMITS.scope}
            onChange={(e) => set("scope", e.target.value)}
            {...described("scope", id("scope-hint"))}
          />
          <small id={id("scope-hint")} className="muted">
            Leer = überall, sonst repo:&lt;Schlüssel&gt; oder model:&lt;Alias&gt;.
          </small>
          <FieldError id={id("scope-err")} error={errors.scope} />
        </div>
      </div>
      <div className="field">
        <label htmlFor={id("title")}>Titel *</label>
        <input
          id={id("title")}
          type="text"
          value={values.title}
          maxLength={MEMORY_LIMITS.title}
          onChange={(e) => set("title", e.target.value)}
          required
          {...described("title")}
        />
        <FieldError id={id("title-err")} error={errors.title} />
      </div>
      <div className="field">
        <label htmlFor={id("content")}>Text (Markdown) *</label>
        <textarea
          id={id("content")}
          rows={8}
          value={values.content}
          maxLength={MEMORY_LIMITS.content}
          onChange={(e) => set("content", e.target.value)}
          required
          {...described("content", id("content-hint"))}
        />
        <small id={id("content-hint")} className="muted">
          Was gilt, woran man es erkennt und was hilft. Modelle sehen nur aktive Einträge, gekürzt.
        </small>
        <FieldError id={id("content-err")} error={errors.content} />
      </div>
      <div className="field">
        <label htmlFor={id("tags")}>Schlagwörter</label>
        <input id={id("tags")} type="text" placeholder="ruff, lint" value={values.tags} onChange={(e) => set("tags", e.target.value)} {...described("tags")} />
        <FieldError id={id("tags-err")} error={errors.tags} />
      </div>
      {serverError ? (
        <p className="error-text" role="alert">
          {serverError}
        </p>
      ) : null}
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={busy} data-testid="memory-save">
          {busy ? "Speichert …" : "Speichern"}
        </button>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Abbrechen
        </button>
      </div>
    </form>
  );
}

/** What a model's recall would find for a question (not counted as a recall). */
function SearchProbe() {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<MemorySearchHit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputId = useId();

  const run = async (e: SyntheticEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setHits(await api.memorySearch(query.trim()));
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Was würde ein Modell finden?" id="memory-probe">
      <form className="filters" onSubmit={(e) => void run(e)} role="search" aria-label="Wissen wie ein Modell abfragen">
        <div className="inline-field">
          <label htmlFor={inputId}>Frage</label>
          <input id={inputId} type="search" value={query} maxLength={500} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <button type="submit" className="btn btn-small" disabled={busy || !query.trim()} data-testid="memory-probe-run">
          Abfragen
        </button>
      </form>
      {error ? <p className="error-text">{error}</p> : null}
      {hits ? (
        hits.length ? (
          <ol className="mini-list" data-testid="memory-probe-hits">
            {hits.map((h) => (
              <li key={h.id}>
                <strong>{h.title}</strong> <span className="muted">({MEMORY_KINDS.find((k) => k.value === h.kind)?.label ?? h.kind})</span> –
                Relevanz {h.score.toFixed(2)}
                {h.scope ? <span className="muted"> · {h.scope}</span> : null}
              </li>
            ))}
          </ol>
        ) : (
          <Empty>Kein aktiver Eintrag passt.</Empty>
        )
      ) : (
        <p className="muted">Zeigt die aktiven Einträge, die ein Modell mit recall für diese Frage bekäme.</p>
      )}
    </Section>
  );
}

export function Wissen() {
  const { search, navigate } = useRouter();
  const kind = search.get("kind") ?? "";
  const status = search.get("status") ?? "";
  const q = search.get("q") ?? "";
  const [text, setText] = useState(q);
  const [editing, setEditing] = useState<MemoryEntry | "new" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const kindId = useId();
  const statusId = useId();
  const textId = useId();
  const entries = useApi(
    `memory:${kind}:${status}:${q}`,
    (s) => api.memory({ kind: kind || undefined, status: status || undefined, q: q || undefined }, s),
    { pollMs: 30000, version },
  );

  const setQuery = (next: { kind?: string; status?: string; q?: string }) => {
    const p = new URLSearchParams();
    const values = { kind, status, q, ...next };
    for (const [key, value] of Object.entries(values)) if (value) p.set(key, value);
    const qs = p.toString();
    navigate(`/wissen${qs ? `?${qs}` : ""}`, { replace: true });
  };

  const saved = (entry: MemoryEntry) => {
    setNotice(`„${entry.title}“ gespeichert (${memoryStatusLabel(entry.status)}).`);
    setEditing(null);
    setVersion((v) => v + 1);
  };

  const setStatus = async (entry: MemoryEntry, next: MemoryStatus) => {
    setActionError(null);
    try {
      saved(await api.updateMemory(entry.id, { status: next }));
    } catch (err) {
      setActionError(describeError(err));
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>Wissen</h1>
        <button
          type="button"
          className="btn btn-primary"
          aria-expanded={editing === "new"}
          onClick={() => {
            setNotice(null);
            setEditing(editing === "new" ? null : "new");
          }}
          data-testid="memory-new"
        >
          {editing === "new" ? "Formular schließen" : "Neuer Eintrag"}
        </button>
      </div>
      <p className="muted">
        Was Hermclaw aus Jobs, Recherchen und Vorfällen gelernt hat. Planer und Coder sehen nur aktive Einträge; Kandidaten warten auf weitere
        Belege oder auf deine Freigabe. Nichts wird gelöscht – falsche Einträge werden zurückgezogen.
      </p>
      {notice ? (
        <p className="success-box" role="status" data-testid="memory-notice">
          {notice}
        </p>
      ) : null}
      {editing ? (
        <Section title={editing === "new" ? "Neuer Eintrag" : `Bearbeiten: ${editing.title}`} id="memory-edit">
          <MemoryForm key={editing === "new" ? "new" : editing.id} entry={editing === "new" ? null : editing} onSaved={saved} onCancel={() => setEditing(null)} />
        </Section>
      ) : null}
      <Section
        title="Einträge"
        id="memory-list"
        actions={
          <form
            className="filters"
            role="search"
            aria-label="Einträge filtern"
            onSubmit={(e) => {
              e.preventDefault();
              setQuery({ q: text.trim() });
            }}
          >
            <div className="inline-field">
              <label htmlFor={kindId}>Art</label>
              <select id={kindId} value={kind} onChange={(e) => setQuery({ kind: e.target.value })}>
                <option value="">Alle</option>
                {MEMORY_KINDS.map((k) => (
                  <option key={k.value} value={k.value}>
                    {k.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="inline-field">
              <label htmlFor={statusId}>Status</label>
              <select id={statusId} value={status} onChange={(e) => setQuery({ status: e.target.value })}>
                <option value="">Alle</option>
                {MEMORY_STATUSES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="inline-field">
              <label htmlFor={textId}>Text</label>
              <input id={textId} type="search" value={text} maxLength={200} onChange={(e) => setText(e.target.value)} />
            </div>
            <button type="submit" className="btn btn-small">
              Filtern
            </button>
          </form>
        }
      >
        {actionError ? (
          <p className="error-text" role="alert">
            {actionError}
          </p>
        ) : null}
        <AsyncBlock result={entries}>
          {(list) =>
            list.length ? (
              <TableWrap label="Wissenseinträge">
                <table className="table" data-testid="memory-table">
                  <thead>
                    <tr>
                      <th scope="col">Titel</th>
                      <th scope="col">Art</th>
                      <th scope="col">Status</th>
                      <th scope="col">Herkunft</th>
                      <th scope="col">Gilt für</th>
                      <th scope="col" title="Jobs, in denen der Vorfall auftrat">
                        Vorkommen
                      </th>
                      <th scope="col" title="Wie oft ein Modell den Eintrag bekam">
                        Abrufe
                      </th>
                      <th scope="col">Job</th>
                      <th scope="col">Aktualisiert</th>
                      <th scope="col">Aktion</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((m) => (
                      <tr key={m.id} data-testid="memory-row" className={m.status === "retired" ? "row-muted" : undefined}>
                        <th scope="row">
                          {m.title}
                          <Excerpt text={m.content} label="Text" />
                          {m.tags.length ? (
                            <p>
                              {m.tags.map((t) => (
                                <span key={t} className="tag">
                                  {t}
                                </span>
                              ))}
                            </p>
                          ) : null}
                          {m.signature ? (
                            <p className="muted">
                              Signatur: <code>{m.signature}</code>
                            </p>
                          ) : null}
                          {Object.keys(m.evidence).length ? <JsonView value={m.evidence} label="Belege" /> : null}
                          {!m.embedded ? <p className="muted">ohne Vektor – nur über Wörter auffindbar</p> : null}
                        </th>
                        <td>{m.kind_label}</td>
                        <td>
                          <Badge tone={memoryStatusTone(m.status)}>{memoryStatusLabel(m.status)}</Badge>
                        </td>
                        <td>{MEMORY_SOURCES[m.source] ?? m.source}</td>
                        <td>{m.scope ?? "überall"}</td>
                        <td className="num">{m.occurrences}</td>
                        <td className="num">{m.recalls}</td>
                        <td>{m.job_id ? <Link to={`/jobs/${m.job_id}`}>{shortId(m.job_id)}</Link> : "–"}</td>
                        <td>{formatDateTime(m.updated_at)}</td>
                        <td>
                          <div className="controls">
                            <button
                              type="button"
                              className="btn btn-small"
                              onClick={() => {
                                setNotice(null);
                                setEditing(m);
                              }}
                            >
                              Bearbeiten
                            </button>
                            {m.status !== "active" ? (
                              <button type="button" className="btn btn-small" onClick={() => void setStatus(m, "active")} data-testid="memory-activate">
                                Aktivieren
                              </button>
                            ) : null}
                            {m.status !== "retired" ? (
                              <button
                                type="button"
                                className="btn btn-small btn-danger-outline"
                                onClick={() => void setStatus(m, "retired")}
                                data-testid="memory-retire"
                              >
                                Zurückziehen
                              </button>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            ) : (
              <Empty>Keine Einträge.</Empty>
            )
          }
        </AsyncBlock>
      </Section>
      <SearchProbe />
    </div>
  );
}
