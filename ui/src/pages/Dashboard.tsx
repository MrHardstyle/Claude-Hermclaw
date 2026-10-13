import { api } from "../api/client";
import type { Job, ResearchRun } from "../api/types";
import { gpuUsage, researchTotals } from "../lib/aggregate";
import { isErrorEvent, statusText, summarizeEvent } from "../lib/events";
import { formatDateTime, formatDuration, formatNumber, formatRelative, formatTime } from "../lib/format";
import { bugSeverityTone, CLOSED_BUG_STATUSES, JOB_STATUS_LABELS, jobTone } from "../lib/labels";
import { Link } from "../lib/router";
import { useApi, useNow, useThrottled } from "../lib/useApi";
import { STREAM_STATE_LABELS, useGlobalEvents } from "../lib/useEvents";
import { mapLimit } from "../lib/useStepDetails";
import { AsyncBlock, Badge, Empty, JobStatusBadge, Meter, Section, SeverityBadge, Stat, TableWrap, WorkerStateBadge } from "../components/ui";

const ACTIVE = new Set([
  "inventory",
  "discovering",
  "researching",
  "planning",
  "waking_worker",
  "running",
  "testing",
  "verifying",
  "reviewing",
  "correcting",
  "replanning",
  "committing",
  "deploying",
]);
const WAITING = new Set(["queued", "waiting_for_resources", "waiting_for_worker", "waiting_for_user", "blocked"]);
const RESEARCH_JOBS = 10;

function sum(rec: Record<string, number>, pred: (k: string) => boolean): number {
  return Object.entries(rec).reduce((s, [k, v]) => (pred(k) ? s + v : s), 0);
}

function JobMiniList({ jobs, empty, testId }: { jobs: Job[]; empty: string; testId: string }) {
  if (!jobs.length) return <Empty>{empty}</Empty>;
  return (
    <ul className="mini-list" data-testid={testId}>
      {jobs.map((j) => (
        <li key={j.id}>
          <Link to={`/jobs/${j.id}`}>{j.title}</Link> <JobStatusBadge status={j.status} />
          <span className="muted">
            {" "}
            Prio {j.priority} · {formatDateTime(j.created_at)}
          </span>
          {j.error_message ? <p className="error-text">{j.error_code ? `${j.error_code}: ` : ""}{j.error_message}</p> : null}
        </li>
      ))}
    </ul>
  );
}

export function Dashboard() {
  const live = useGlobalEvents(300);
  const now = useNow(5000);
  // job-level live events trigger a (throttled) refresh of the job overviews
  const jobEventCount = live.events.filter((e) => e.event_type.startsWith("job.")).length;
  const jobVersion = useThrottled(jobEventCount, 2000);
  const workerVersion = useThrottled(live.events.filter((e) => e.event_type.startsWith("worker.")).length, 2000);
  const resourceVersion = useThrottled(live.events.filter((e) => e.event_type.startsWith("resource.")).length, 2000);

  const stats = useApi("stats", (s) => api.stats(s), { pollMs: 10000, version: jobVersion });
  const queue = useApi("queue", (s) => api.jobs({ status: "queued", limit: 10 }, s), { pollMs: 10000, version: jobVersion });
  const recent = useApi("recent-jobs", (s) => api.jobs({ limit: 10 }, s), { pollMs: 10000, version: jobVersion });
  const failed = useApi("failed-jobs", (s) => api.jobs({ status: "failed", limit: 5 }, s), { pollMs: 15000, version: jobVersion });
  const workers = useApi("workers", (s) => api.workers(s), { pollMs: 10000, version: workerVersion });
  const models = useApi("models", (s) => api.models(s), { pollMs: 30000 });
  const resources = useApi("resources", (s) => api.resources(s), { pollMs: 5000, version: resourceVersion });
  const bugs = useApi("bugs", (s) => api.bugs(undefined, s), { pollMs: 30000 });
  const recentIds = recent.data?.items.slice(0, RESEARCH_JOBS).map((j) => j.id) ?? null;
  const research = useApi(
    recentIds ? `research:${recentIds.join(",")}` : null,
    async (s) => (await mapLimit<string, ResearchRun[]>(recentIds ?? [], 4, (id) => api.research(id, s))).flat(),
    { pollMs: 60000 },
  );

  const statusLines = live.events.filter((e) => statusText(e) !== null).slice(-6).reverse();
  const liveErrors = live.events.filter(isErrorEvent).slice(-10).reverse();

  return (
    <div className="page">
      <div className="page-head">
        <h1>Dashboard</h1>
        <Badge tone={live.stream.state === "open" ? "success" : live.stream.state === "reconnecting" ? "warning" : "neutral"} testId="global-stream-state">
          {STREAM_STATE_LABELS[live.stream.state]}
        </Badge>
      </div>

      <section className="card live-ticker" aria-labelledby="ticker-h">
        <h2 id="ticker-h" className="sr-only">
          Live-Status
        </h2>
        {statusLines.length ? (
          <ul aria-live="polite" data-testid="dashboard-status-lines">
            {statusLines.map((e) => (
              <li key={e.sequence}>
                <time dateTime={e.ts}>{formatTime(e.ts)}</time> <strong>{statusText(e)}</strong>
                {e.job_id ? (
                  <>
                    {" "}
                    <Link to={`/jobs/${e.job_id}/events`} className="muted">
                      Job öffnen
                    </Link>
                  </>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">Keine neuen Statusmeldungen seit dem Öffnen der Seite.</p>
        )}
      </section>

      <Section title="Jobs nach Status" id="dash-jobs">
        <AsyncBlock result={stats}>
          {(st) => (
            <>
              <div className="stats-row">
                <Stat label="Jobs gesamt" value={formatNumber(st.jobs_total)} testId="stat-jobs-total" />
                <Stat label="Aktiv" value={formatNumber(sum(st.jobs_by_status, (k) => ACTIVE.has(k)))} tone="active" testId="stat-jobs-active" />
                <Stat label="Wartend" value={formatNumber(sum(st.jobs_by_status, (k) => WAITING.has(k)))} tone="info" testId="stat-jobs-waiting" />
                <Stat label="Erfolgreich" value={formatNumber(st.jobs_by_status.succeeded ?? 0)} tone="success" testId="stat-jobs-succeeded" />
                <Stat label="Fehlgeschlagen" value={formatNumber(st.jobs_by_status.failed ?? 0)} tone="danger" testId="stat-jobs-failed" />
              </div>
              <ul className="status-chips" aria-label="Jobs je Status" data-testid="jobs-by-status">
                {Object.entries(st.jobs_by_status)
                  .sort((a, b) => b[1] - a[1])
                  .map(([k, v]) => (
                    <li key={k}>
                      <Link to={`/jobs?status=${encodeURIComponent(k)}`}>
                        <Badge tone={jobTone(k)}>{JOB_STATUS_LABELS[k] ?? k}</Badge> <span className="chip-count">{v}</span>
                      </Link>
                    </li>
                  ))}
              </ul>
            </>
          )}
        </AsyncBlock>
      </Section>

      <div className="grid-2">
        <Section title="Warteschlange" id="dash-queue" actions={<Link to="/jobs?status=queued">Alle</Link>}>
          <AsyncBlock result={queue}>{(q) => <JobMiniList jobs={q.items} empty="Keine Jobs in der Warteschlange." testId="queue-list" />}</AsyncBlock>
        </Section>
        <Section title="Neueste Jobs" id="dash-recent" actions={<Link to="/jobs">Alle Jobs</Link>}>
          <AsyncBlock result={recent}>{(r) => <JobMiniList jobs={r.items.slice(0, 6)} empty="Noch keine Jobs." testId="recent-jobs" />}</AsyncBlock>
        </Section>
      </div>

      <div className="grid-2">
        <Section title="Worker" id="dash-workers" actions={<Link to="/workers">Details</Link>}>
          <AsyncBlock result={workers}>
            {(ws) =>
              ws.length ? (
                <ul className="mini-list" data-testid="dashboard-workers">
                  {ws.map((w) => (
                    <li key={w.id} data-testid="dashboard-worker" data-worker={w.id}>
                      <WorkerStateBadge state={w.display_state || w.state} /> <strong>{w.id}</strong>{" "}
                      <span className="muted">
                        {w.kind} · {w.address} · Heartbeat {formatRelative(w.last_heartbeat_at, now)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>Keine Worker registriert.</Empty>
              )
            }
          </AsyncBlock>
        </Section>

        <Section title="GPU / Ressourcen" id="dash-gpu" actions={<Link to="/resources">Details</Link>}>
          <AsyncBlock result={resources}>
            {(res) => {
              const usage = gpuUsage(res.active_leases, models.data?.profiles ?? [], res.model_host_capacity_gb);
              return (
                <div data-testid="dashboard-resources">
                  <p>
                    Modell-Host-Speicher: <strong data-testid="gpu-used">{formatNumber(usage.usedGb)}</strong> von {formatNumber(usage.capacityGb)} GB
                    belegt
                  </p>
                  <Meter value={usage.capacityGb > 0 ? usage.usedGb / usage.capacityGb : 0} label="GPU-Speicher belegt" />
                  <div className="stats-row">
                    <Stat label="Aktive Leases" value={res.active_leases.length} testId="stat-leases" />
                    <Stat label="Wartende Anfragen" value={res.waiting_requests.length} tone={res.waiting_requests.length ? "warning" : undefined} testId="stat-waiting" />
                  </div>
                  {res.active_leases.length ? (
                    <ul className="mini-list">
                      {res.active_leases.slice(0, 6).map((l) => (
                        <li key={l.id}>
                          <code>{l.resource}</code> <span className="muted">({l.resource_group})</span> – {l.owner_kind} · Prio {l.priority}
                          {l.owner_job_id ? (
                            <>
                              {" "}
                              · <Link to={`/jobs/${l.owner_job_id}/resources`}>Job</Link>
                            </>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              );
            }}
          </AsyncBlock>
        </Section>
      </div>

      <Section title="Modelle" id="dash-models">
        <AsyncBlock result={models}>
          {(m) => (
            <TableWrap label="Modellprofile">
              <table className="table" data-testid="models-table">
                <thead>
                  <tr>
                    <th scope="col">Alias</th>
                    <th scope="col">Rolle</th>
                    <th scope="col">Modell</th>
                    <th scope="col">Host</th>
                    <th scope="col">Speicher</th>
                    <th scope="col">Aufrufe</th>
                    <th scope="col">Ø Latenz</th>
                    <th scope="col">Fehler</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {m.profiles.map((p) => (
                    <tr key={p.alias}>
                      <th scope="row">{p.alias}</th>
                      <td>{p.role}</td>
                      <td>
                        <code>{p.model}</code>
                      </td>
                      <td>{p.host ?? "–"}</td>
                      <td>{p.memory_gb !== undefined ? `${formatNumber(p.memory_gb)} GB` : "–"}</td>
                      <td>{formatNumber(p.stats.calls)}</td>
                      <td>{p.stats.avg_latency_ms !== undefined ? formatDuration(p.stats.avg_latency_ms) : "–"}</td>
                      <td>{formatNumber(p.stats.errors ?? 0)}</td>
                      <td>{p.enabled === false ? <Badge tone="muted">deaktiviert</Badge> : <Badge tone="success">aktiv</Badge>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          )}
        </AsyncBlock>
      </Section>

      <div className="grid-3">
        <Section title="Letzte Fehler" id="dash-errors">
          <AsyncBlock result={failed}>
            {(f) => (
              <div data-testid="dashboard-errors">
                <JobMiniList jobs={f.items} empty="Keine fehlgeschlagenen Jobs." testId="failed-jobs" />
                {liveErrors.length ? (
                  <>
                    <h3>Live seit Seitenaufruf</h3>
                    <ul className="mini-list">
                      {liveErrors.map((e) => (
                        <li key={e.sequence}>
                          <SeverityBadge severity={e.severity} /> <code>{e.event_type}</code> {summarizeEvent(e)}{" "}
                          {e.job_id ? <Link to={`/jobs/${e.job_id}/errors`}>Job</Link> : null}
                        </li>
                      ))}
                    </ul>
                  </>
                ) : null}
              </div>
            )}
          </AsyncBlock>
        </Section>

        <Section title="Research" id="dash-research">
          <AsyncBlock result={research} loadingLabel="Research wird geladen …">
            {(runs) => {
              const t = researchTotals(runs);
              return (
                <div data-testid="dashboard-research">
                  <div className="stats-row">
                    <Stat label="Research-Läufe" value={t.runs} testId="stat-research-runs" />
                    <Stat label="Quellen" value={t.sources} testId="stat-research-sources" />
                    <Stat label="Aussagen" value={t.claims} />
                    <Stat label="Widersprüche" value={t.contradictions} tone={t.contradictions ? "warning" : undefined} />
                  </div>
                  <p className="muted">Über die letzten {RESEARCH_JOBS} Jobs.</p>
                </div>
              );
            }}
          </AsyncBlock>
        </Section>

        <Section title="Bugs" id="dash-bugs" actions={<Link to="/bugs">Alle</Link>}>
          <AsyncBlock result={bugs}>
            {(bs) => {
              const open = bs.filter((b) => !CLOSED_BUG_STATUSES.has(b.status));
              return (
                <div data-testid="dashboard-bugs">
                  <div className="stats-row">
                    <Stat label="Offen" value={open.length} tone={open.length ? "warning" : "success"} testId="stat-bugs-open" />
                    <Stat label="Blockierend" value={open.filter((b) => b.blocking).length} tone={open.some((b) => b.blocking) ? "danger" : undefined} />
                    <Stat label="Gesamt" value={bs.length} />
                  </div>
                  <ul className="mini-list">
                    {open.slice(0, 5).map((b) => (
                      <li key={b.id}>
                        <Badge tone={bugSeverityTone(b.severity)}>{b.severity}</Badge> <strong>{b.bug_key}</strong> {b.title}
                      </li>
                    ))}
                  </ul>
                </div>
              );
            }}
          </AsyncBlock>
        </Section>
      </div>
    </div>
  );
}
