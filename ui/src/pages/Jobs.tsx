import { useId, useState } from "react";
import { api } from "../api/client";
import { JOB_STATUSES, type Job } from "../api/types";
import { JobCreateForm } from "../components/JobCreateForm";
import { AsyncBlock, Empty, JobStatusBadge, Section, TableWrap } from "../components/ui";
import { formatDateTime, formatRelative, shortId } from "../lib/format";
import { jobStatusLabel } from "../lib/labels";
import { Link, useRouter } from "../lib/router";
import { useApi, useNow } from "../lib/useApi";

const PAGE_SIZE = 25;

export function Jobs() {
  const { search, navigate } = useRouter();
  const status = search.get("status") ?? "";
  const page = Math.max(0, Number.parseInt(search.get("page") ?? "0", 10) || 0);
  const [showForm, setShowForm] = useState(search.get("new") === "1");
  const [created, setCreated] = useState<Job | null>(null);
  const [version, setVersion] = useState(0);
  const filterId = useId();
  const now = useNow(10000);

  const jobs = useApi(
    `jobs:${status}:${page}`,
    (s) => api.jobs({ status: status || undefined, limit: PAGE_SIZE, offset: page * PAGE_SIZE }, s),
    { pollMs: 5000, version },
  );
  const repos = useApi("repositories", (s) => api.repositories(s));
  const repoName = new Map((repos.data ?? []).map((r) => [r.id, r.name]));

  const setQuery = (next: { status?: string; page?: number }) => {
    const q = new URLSearchParams();
    const st = next.status ?? status;
    const pg = next.page ?? 0;
    if (st) q.set("status", st);
    if (pg > 0) q.set("page", String(pg));
    const qs = q.toString();
    navigate(`/jobs${qs ? `?${qs}` : ""}`, { replace: true });
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>Jobs</h1>
        <button
          type="button"
          className="btn btn-primary"
          aria-expanded={showForm}
          aria-controls="new-job"
          onClick={() => setShowForm((v) => !v)}
          data-testid="toggle-job-form"
        >
          {showForm ? "Formular schließen" : "Neuer Job"}
        </button>
      </div>

      {created ? (
        <p className="success-box" role="status" data-testid="job-created">
          Job <Link to={`/jobs/${created.id}`}>{created.title}</Link> wurde angelegt ({jobStatusLabel(created.status)}).
        </p>
      ) : null}

      {showForm ? (
        <Section title="Neuen Job anlegen" id="new-job">
          <JobCreateForm
            repositories={repos.data ?? []}
            onCreated={(job) => {
              setCreated(job);
              setShowForm(false);
              if (status || page) setQuery({ status: "", page: 0 });
              setVersion((v) => v + 1);
              repos.reload();
            }}
          />
        </Section>
      ) : null}

      <Section
        title="Alle Jobs"
        id="job-list"
        actions={
          <div className="inline-field">
            <label htmlFor={filterId}>Status</label>
            <select
              id={filterId}
              value={status}
              onChange={(e) => setQuery({ status: e.target.value, page: 0 })}
              data-testid="status-filter"
            >
              <option value="">Alle</option>
              {JOB_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {jobStatusLabel(s)}
                </option>
              ))}
            </select>
          </div>
        }
      >
        <AsyncBlock result={jobs}>
          {(list) =>
            list.items.length === 0 ? (
              <Empty>{status ? `Keine Jobs mit Status „${jobStatusLabel(status)}“.` : "Noch keine Jobs angelegt."}</Empty>
            ) : (
              <>
                <TableWrap label="Jobliste">
                  <table className="table" data-testid="jobs-table">
                    <thead>
                      <tr>
                        <th scope="col">Titel</th>
                        <th scope="col">Status</th>
                        <th scope="col">Priorität</th>
                        <th scope="col">Repository</th>
                        <th scope="col">Plan</th>
                        <th scope="col">Erstellt</th>
                        <th scope="col">Aktualisiert</th>
                      </tr>
                    </thead>
                    <tbody>
                      {list.items.map((j) => (
                        <tr key={j.id} data-testid="job-row" data-job={j.id}>
                          <th scope="row">
                            <Link to={`/jobs/${j.id}`}>{j.title}</Link>
                            <span className="muted mono"> {shortId(j.id)}</span>
                          </th>
                          <td>
                            <JobStatusBadge status={j.status} />
                            {j.pause_requested ? <span className="muted"> (Pause angefordert)</span> : null}
                          </td>
                          <td>{j.priority}</td>
                          <td>
                            {j.repository_id ? (repoName.get(j.repository_id) ?? shortId(j.repository_id)) : "–"}
                            {j.base_branch ? <span className="muted"> @ {j.base_branch}</span> : null}
                          </td>
                          <td>{j.current_plan_version !== null ? `v${j.current_plan_version}` : "–"}</td>
                          <td>{formatDateTime(j.created_at)}</td>
                          <td title={formatDateTime(j.updated_at)}>{formatRelative(j.updated_at, now)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableWrap>
                <nav className="pager" aria-label="Seiten">
                  <button type="button" className="btn btn-small" disabled={page === 0} onClick={() => setQuery({ page: page - 1 })}>
                    Zurück
                  </button>
                  <span>
                    Seite {page + 1} von {Math.max(1, Math.ceil(list.total / PAGE_SIZE))} · {list.total} Jobs
                  </span>
                  <button
                    type="button"
                    className="btn btn-small"
                    disabled={(page + 1) * PAGE_SIZE >= list.total}
                    onClick={() => setQuery({ page: page + 1 })}
                  >
                    Weiter
                  </button>
                </nav>
              </>
            )
          }
        </AsyncBlock>
      </Section>
    </div>
  );
}
