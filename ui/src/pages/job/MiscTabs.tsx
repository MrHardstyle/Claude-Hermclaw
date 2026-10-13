import { api } from "../../api/client";
import type { HermEvent } from "../../api/types";
import { DownloadButton } from "../../components/Artifacts";
import { EventList } from "../../components/EventList";
import { MediaGallery } from "../../components/MediaGallery";
import { AsyncBlock, Badge, Empty, KeyValue, Section, StepStatusBadge, TableWrap } from "../../components/ui";
import { eventsWithPrefix, leasesForJob } from "../../lib/aggregate";
import { isErrorEvent } from "../../lib/events";
import { formatBytes, formatDateTime, shortId } from "../../lib/format";
import { useApi, useThrottled } from "../../lib/useApi";
import type { JobTabProps } from "./types";

function EventSection({ title, id, events, stepKeys, empty }: { title: string; id: string; events: HermEvent[]; stepKeys: Map<string, string>; empty: string }) {
  return (
    <Section title={`${title} (${String(events.length)})`} id={id}>
      {events.length ? <EventList events={events} stepKeys={stepKeys} label={title} /> : <Empty>{empty}</Empty>}
    </Section>
  );
}

export function RepositoryTab({ job, events, stepKeys }: JobTabProps) {
  const repos = useApi("repositories", (s) => api.repositories(s));
  const repoEvents = eventsWithPrefix(events, ["repo", "repository"]);
  return (
    <div className="stack">
      <Section title="Repository" id="repo-main">
        {job.repository_id === null ? (
          <Empty>Dieser Job arbeitet ohne Repository.</Empty>
        ) : (
          <AsyncBlock result={repos}>
            {(rs) => {
              const r = rs.find((x) => x.id === job.repository_id);
              if (!r) return <Empty>Repository {shortId(job.repository_id)} nicht gefunden.</Empty>;
              return (
                <KeyValue
                  items={[
                    ["Name", <strong key="n">{r.name}</strong>],
                    ["URL", <code key="u">{r.url}</code>],
                    ["Standard-Branch", r.default_branch],
                    ["Basis-Branch des Jobs", job.base_branch],
                    ["Anbieter", r.provider],
                    ["Geschützte Branches", r.protected_branches.length ? r.protected_branches.join(", ") : "–"],
                  ]}
                />
              );
            }}
          </AsyncBlock>
        )}
      </Section>
      <EventSection title="Inventar, Suche und Index" id="repo-events" events={repoEvents} stepKeys={stepKeys} empty="Noch keine Repository-Analyse." />
    </div>
  );
}

export function ResourcesTab({ job, events, stepKeys }: JobTabProps) {
  const resEvents = eventsWithPrefix(events, ["resource"]);
  const version = useThrottled(resEvents.length, 2000);
  const res = useApi("resources", (s) => api.resources(s), { pollMs: 5000, version });
  return (
    <div className="stack">
      <Section title="Ressourcen dieses Jobs" id="res-main">
        <AsyncBlock result={res}>
          {(r) => {
            const leases = leasesForJob(r.active_leases, job.id);
            const waiting = leasesForJob(r.waiting_requests, job.id);
            return (
              <>
                <h3>Aktive Leases ({leases.length})</h3>
                {leases.length ? (
                  <TableWrap label="Aktive Leases des Jobs">
                    <table className="table" data-testid="job-leases">
                      <thead>
                        <tr>
                          <th scope="col">Ressource</th>
                          <th scope="col">Gruppe</th>
                          <th scope="col">Art</th>
                          <th scope="col">Halter</th>
                          <th scope="col">Priorität</th>
                          <th scope="col">Zustand</th>
                          <th scope="col">Gewicht</th>
                          <th scope="col">Läuft ab</th>
                        </tr>
                      </thead>
                      <tbody>
                        {leases.map((l) => (
                          <tr key={l.id}>
                            <th scope="row">
                              <code>{l.resource}</code>
                            </th>
                            <td>{l.resource_group}</td>
                            <td>{l.owner_kind}</td>
                            <td>{l.holder}</td>
                            <td className="num">{l.priority}</td>
                            <td>
                              <Badge tone={l.state === "preempting" ? "warning" : "active"}>{l.state}</Badge>
                            </td>
                            <td className="num">{l.weight}</td>
                            <td>{formatDateTime(l.expires_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableWrap>
                ) : (
                  <Empty>Der Job hält derzeit keine Ressourcen.</Empty>
                )}
                <h3>Wartende Anfragen ({waiting.length})</h3>
                {waiting.length ? (
                  <ul className="mini-list" data-testid="job-waiting">
                    {waiting.map((w) => (
                      <li key={w.id}>
                        <code>{w.resource}</code> – {w.owner_kind} · Prio {w.priority} · seit {formatDateTime(w.created_at)}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Empty>Keine wartenden Anfragen.</Empty>
                )}
              </>
            );
          }}
        </AsyncBlock>
      </Section>
      <EventSection title="Ressourcen-Verlauf" id="res-events" events={resEvents} stepKeys={stepKeys} empty="Keine Ressourcen-Events." />
    </div>
  );
}

export function ArtifactsTab({ job, stepKeys, events }: JobTabProps) {
  const version = useThrottled(events.length, 5000);
  const arts = useApi(`artifacts:${job.id}`, (s) => api.artifacts(job.id, s), { version });
  return (
    <Section title="Artefakte" id="art-main">
      <AsyncBlock result={arts}>
        {(list) =>
          list.length ? (
            <TableWrap label="Artefakte">
              <table className="table" data-testid="artifacts-table">
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Art</th>
                    <th scope="col">Medientyp</th>
                    <th scope="col">Größe</th>
                    <th scope="col">Schritt</th>
                    <th scope="col">SHA-256</th>
                    <th scope="col">Erstellt</th>
                    <th scope="col">Download</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((a) => (
                    <tr key={a.id} data-testid="artifact-row">
                      <th scope="row">{a.name}</th>
                      <td>{a.kind}</td>
                      <td>{a.media_type}</td>
                      <td>{formatBytes(a.size_bytes)}</td>
                      <td>{a.step_id ? (stepKeys.get(a.step_id) ?? shortId(a.step_id)) : "–"}</td>
                      <td>
                        <code title={a.sha256}>{shortId(a.sha256, 12)}</code>
                      </td>
                      <td>{formatDateTime(a.created_at)}</td>
                      <td>
                        <DownloadButton artifact={a} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          ) : (
            <Empty>Keine Artefakte.</Empty>
          )
        }
      </AsyncBlock>
    </Section>
  );
}

export function MediaTab({ job, events, stepKeys }: JobTabProps) {
  const mediaEvents = eventsWithPrefix(events, ["media"]);
  return (
    <div className="stack">
      <Section title="Medien" id="media-main">
        <MediaGallery jobId={job.id} version={mediaEvents.length} />
      </Section>
      <EventSection title="Medien-Events" id="media-events" events={mediaEvents} stepKeys={stepKeys} empty="Keine Medien-Events." />
    </div>
  );
}

export function GitTab({ events, stepKeys }: JobTabProps) {
  const gitEvents = eventsWithPrefix(events, ["git", "checkpoint"]);
  const commits = gitEvents.filter((e) => e.event_type === "git.commit.created");
  return (
    <div className="stack">
      {commits.length ? (
        <Section title={`Commits (${String(commits.length)})`} id="git-commits">
          <ul className="mini-list" data-testid="git-commits">
            {commits.map((e) => {
              const p = e.payload;
              const sha = typeof p.sha === "string" ? p.sha : typeof p.commit === "string" ? p.commit : "";
              return (
                <li key={e.sequence}>
                  <code>{sha.slice(0, 12) || "–"}</code> {typeof p.message === "string" ? p.message : ""}{" "}
                  {typeof p.branch === "string" ? <span className="muted">auf {p.branch}</span> : null}
                  <span className="muted"> · {formatDateTime(e.ts)}</span>
                </li>
              );
            })}
          </ul>
        </Section>
      ) : null}
      <EventSection title="Git-Operationen" id="git-events" events={gitEvents} stepKeys={stepKeys} empty="Noch keine Git-Operationen." />
    </div>
  );
}

export function DeploymentTab({ events, stepKeys }: JobTabProps) {
  return (
    <EventSection
      title="Deployment"
      id="deploy-events"
      events={eventsWithPrefix(events, ["deployment"])}
      stepKeys={stepKeys}
      empty="Für diesen Job wurde nichts deployt."
    />
  );
}

export function ErrorsTab({ job, events, stepKeys }: JobTabProps) {
  const problems = events.filter(isErrorEvent);
  const failedSteps = job.steps.filter((s) => s.error_message || s.status === "failed" || s.status === "blocked");
  return (
    <div className="stack">
      <Section title="Job-Fehler" id="err-job">
        {job.error_message || job.error_code ? (
          <p className="error-box" data-testid="job-error">
            <strong>{job.error_code ?? "Fehler"}:</strong> {job.error_message ?? ""}
          </p>
        ) : (
          <Empty>Der Job hat keinen Fehler gemeldet.</Empty>
        )}
        {failedSteps.length ? (
          <>
            <h3>Betroffene Schritte</h3>
            <ul className="mini-list" data-testid="failed-steps">
              {failedSteps.map((s) => (
                <li key={s.id}>
                  <strong>{s.step_key}</strong> {s.title} <StepStatusBadge status={s.status} />
                  {s.error_message ? (
                    <p className="error-text">
                      {s.error_code ? `${s.error_code}: ` : ""}
                      {s.error_message}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </Section>
      <Section title={`Fehler und Warnungen (${String(problems.length)})`} id="err-events">
        {problems.length ? (
          <div data-testid="error-events">
            <EventList events={problems} stepKeys={stepKeys} label="Fehler- und Warn-Events" />
          </div>
        ) : (
          <Empty>Keine Fehler- oder Warn-Events.</Empty>
        )}
      </Section>
    </div>
  );
}
