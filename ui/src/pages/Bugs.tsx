import { useId } from "react";
import { api } from "../api/client";
import { AsyncBlock, Badge, Empty, Excerpt, ResultBadge, Section, TableWrap } from "../components/ui";
import { boolLabel, formatDateTime, shortId } from "../lib/format";
import { bugSeverityTone } from "../lib/labels";
import { Link, useRouter } from "../lib/router";
import { useApi } from "../lib/useApi";

const STATUSES = ["open", "in_progress", "fixed", "verified", "wontfix"];

export function Bugs() {
  const { search, navigate } = useRouter();
  const status = search.get("status") ?? "";
  const filterId = useId();
  const bugs = useApi(`bugs:${status}`, (s) => api.bugs(status || undefined, s), { pollMs: 30000 });
  return (
    <div className="page">
      <h1>Bugs</h1>
      <Section
        title="Bug-Register"
        id="bugs-list"
        actions={
          <div className="inline-field">
            <label htmlFor={filterId}>Status</label>
            <select
              id={filterId}
              value={status}
              onChange={(e) => navigate(e.target.value ? `/bugs?status=${e.target.value}` : "/bugs", { replace: true })}
            >
              <option value="">Alle</option>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
        }
      >
        <AsyncBlock result={bugs}>
          {(list) =>
            list.length ? (
              <TableWrap label="Bugs">
                <table className="table" data-testid="bugs-table">
                  <thead>
                    <tr>
                      <th scope="col">Bug</th>
                      <th scope="col">Titel</th>
                      <th scope="col">Schwere</th>
                      <th scope="col">Komponente</th>
                      <th scope="col">Phase</th>
                      <th scope="col">Status</th>
                      <th scope="col">Blockierend</th>
                      <th scope="col">Job</th>
                      <th scope="col">Aktualisiert</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((b) => (
                      <tr key={b.id} data-testid="bug-row">
                        <th scope="row">{b.bug_key}</th>
                        <td>
                          {b.title}
                          <Excerpt text={b.reproduction} label="Reproduktion" />
                          <Excerpt text={b.expected} label="Erwartet" />
                          <Excerpt text={b.actual} label="Tatsächlich" />
                          <Excerpt text={b.workaround} label="Workaround" />
                          {b.regression_test ? (
                            <p className="muted">
                              Regressionstest: <code>{b.regression_test}</code>
                            </p>
                          ) : null}
                          {b.fix_commit ? (
                            <p className="muted">
                              Fix: <code>{b.fix_commit}</code>
                            </p>
                          ) : null}
                        </td>
                        <td>
                          <Badge tone={bugSeverityTone(b.severity)}>{b.severity}</Badge>
                        </td>
                        <td>{b.component}</td>
                        <td>{b.phase ?? "–"}</td>
                        <td>
                          <ResultBadge status={b.status} />
                        </td>
                        <td>{b.blocking ? <Badge tone="danger">Ja</Badge> : boolLabel(false)}</td>
                        <td>{b.job_id ? <Link to={`/jobs/${b.job_id}`}>{shortId(b.job_id)}</Link> : "–"}</td>
                        <td>{formatDateTime(b.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            ) : (
              <Empty>Keine Bugs erfasst.</Empty>
            )
          }
        </AsyncBlock>
      </Section>
    </div>
  );
}
