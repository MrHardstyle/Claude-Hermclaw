import { api } from "../../api/client";
import type { ResearchSource } from "../../api/types";
import { AsyncBlock, Badge, Empty, Excerpt, JsonView, Meter, ResultBadge, Section, Stat, TableWrap } from "../../components/ui";
import { researchRunView, researchTotals, safeExternalUrl, type ClaimView } from "../../lib/aggregate";
import { eventsWithPrefix } from "../../lib/aggregate";
import { formatDateTime, formatPercent } from "../../lib/format";
import { useApi, useThrottled } from "../../lib/useApi";
import type { JobTabProps } from "./types";

function sourceAnchor(id: string): string {
  return `source-${id}`;
}

export function SourceLink({ source }: { source: ResearchSource }) {
  const href = safeExternalUrl(source.url);
  const label = source.title || source.url;
  if (!href) return <span title="Keine http(s)-URL – nicht verlinkt">{label}</span>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow" referrerPolicy="no-referrer" data-testid="source-link">
      {label}
      <span className="sr-only"> (öffnet in neuem Tab)</span>
    </a>
  );
}

function ClaimItem({ c }: { c: ClaimView }) {
  return (
    <li className="claim" data-testid="research-claim">
      <p>
        {c.claim.claim}{" "}
        {c.sources.map(({ source, n }) => (
          <a key={source.id} href={`#${sourceAnchor(source.id)}`} className="cite" aria-label={`Quelle ${String(n)}: ${source.title || source.domain}`}>
            [{n}]
          </a>
        ))}
      </p>
      <p className="muted">
        Konfidenz {formatPercent(c.claim.confidence)}
        {c.claim.used_for_decision ? (
          <>
            {" "}
            · <Badge tone="info">für Entscheidung genutzt</Badge>
            {c.claim.decision_ref ? <code> {c.claim.decision_ref}</code> : null}
          </>
        ) : null}
        {c.claim.contradiction_group !== null ? (
          <>
            {" "}
            · <Badge tone="warning">Widerspruchsgruppe {c.claim.contradiction_group}</Badge>
          </>
        ) : null}
        {c.missingSourceIds.length ? ` · ${String(c.missingSourceIds.length)} Quelle(n) nicht gefunden` : ""}
      </p>
    </li>
  );
}

export function ResearchTab({ job, events }: JobTabProps) {
  const version = useThrottled(eventsWithPrefix(events, ["research"]).length, 2000);
  const research = useApi(`research:${job.id}`, (s) => api.research(job.id, s), { version });
  return (
    <AsyncBlock result={research}>
      {(runs) => {
        if (!runs.length) return <Empty>Für diesen Job wurde (noch) kein Research durchgeführt.</Empty>;
        const t = researchTotals(runs);
        return (
          <div className="stack">
            <div className="stats-row" data-testid="research-totals">
              <Stat label="Research-Läufe" value={t.runs} />
              <Stat label="Quellen" value={t.sources} />
              <Stat label="Aussagen" value={t.claims} />
              <Stat label="Widersprüche" value={t.contradictions} tone={t.contradictions ? "warning" : undefined} />
            </div>
            {runs.map((run) => {
              const v = researchRunView(run);
              return (
                <Section key={run.id} title={run.question} id={`research-${run.id}`} actions={<ResultBadge status={run.status} />}>
                  <p className="muted">
                    {run.model_alias ?? "Research"} · {formatDateTime(run.created_at)}
                    {run.finished_at ? ` – ${formatDateTime(run.finished_at)}` : ""}
                  </p>
                  {run.queries.length ? (
                    <p>
                      Suchanfragen:{" "}
                      {run.queries.map((q, i) => (
                        <code key={i} className="tag">
                          {typeof q === "string" ? q : JSON.stringify(q)}
                        </code>
                      ))}
                    </p>
                  ) : null}
                  {run.synthesis ? (
                    <>
                      <h3>Synthese</h3>
                      <p className="prewrap" data-testid="research-synthesis">
                        {run.synthesis}
                      </p>
                    </>
                  ) : null}

                  <h3>Quellen ({v.sources.length})</h3>
                  {v.sources.length ? (
                    <TableWrap label="Quellen">
                      <table className="table" data-testid="sources-table">
                        <thead>
                          <tr>
                            <th scope="col">#</th>
                            <th scope="col">Quelle</th>
                            <th scope="col">Domain</th>
                            <th scope="col">Typ</th>
                            <th scope="col">Autorität</th>
                            <th scope="col">Relevanz</th>
                            <th scope="col">Status</th>
                            <th scope="col">Abgerufen</th>
                          </tr>
                        </thead>
                        <tbody>
                          {v.sources.map(({ source: s, n }) => (
                            <tr key={s.id} id={sourceAnchor(s.id)} data-testid="source-row">
                              <td className="num">{n}</td>
                              <td>
                                <SourceLink source={s} />
                                {s.published_at ? <span className="muted"> · veröffentlicht {formatDateTime(s.published_at)}</span> : null}
                                <Excerpt text={s.excerpt} label="Auszug" />
                                {s.error ? <p className="error-text">{s.error}</p> : null}
                              </td>
                              <td>{s.domain}</td>
                              <td>{s.source_type}</td>
                              <td>
                                <Meter value={s.authority_score} label={`Autorität ${s.domain}`} />
                              </td>
                              <td>
                                <Meter value={s.relevance_score} label={`Relevanz ${s.domain}`} />
                              </td>
                              <td>
                                <ResultBadge status={s.status} />
                              </td>
                              <td>{formatDateTime(s.retrieved_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </TableWrap>
                  ) : (
                    <Empty>Keine Quellen gelesen.</Empty>
                  )}

                  <h3>Aussagen ({v.claims.length})</h3>
                  {v.claims.length ? (
                    <ul className="claims">
                      {v.claims.map((c) => (
                        <ClaimItem key={c.claim.id} c={c} />
                      ))}
                    </ul>
                  ) : (
                    <Empty>Keine Aussagen extrahiert.</Empty>
                  )}

                  {v.contradictionGroups.length || run.contradictions.length ? (
                    <>
                      <h3>Widersprüche</h3>
                      <div data-testid="research-contradictions">
                        {v.contradictionGroups.map((g) => (
                          <div key={g.group} className="contradiction">
                            <strong>Gruppe {g.group}</strong>
                            <ul className="claims">
                              {g.claims.map((c) => (
                                <ClaimItem key={c.claim.id} c={c} />
                              ))}
                            </ul>
                          </div>
                        ))}
                        {run.contradictions.length ? <JsonView value={run.contradictions} label="Widerspruchsanalyse (JSON)" /> : null}
                      </div>
                    </>
                  ) : null}
                </Section>
              );
            })}
          </div>
        );
      }}
    </AsyncBlock>
  );
}
