import { api } from "../api/client";
import { AsyncBlock, Badge, Empty, Meter, Section, Stat, TableWrap } from "../components/ui";
import { gpuUsage } from "../lib/aggregate";
import { boolLabel, formatDateTime, formatNumber, formatRelative, shortId } from "../lib/format";
import { resultLabel } from "../lib/labels";
import { Link } from "../lib/router";
import { useApi, useNow } from "../lib/useApi";

export function Resources() {
  const now = useNow(5000);
  const res = useApi("resources", (s) => api.resources(s), { pollMs: 5000 });
  const models = useApi("models", (s) => api.models(s), { pollMs: 60000 });

  return (
    <div className="page">
      <h1>Ressourcen</h1>
      <AsyncBlock result={res}>
        {(r) => {
          const profiles = models.data?.profiles ?? [];
          const usage = gpuUsage(r.active_leases, profiles, r.model_host_capacity_gb);
          return (
            <div className="stack">
              <Section title="GPU / Modell-Host" id="res-gpu">
                <div className="stats-row" data-testid="resources-summary">
                  <Stat label="Kapazität" value={`${formatNumber(usage.capacityGb)} GB`} />
                  <Stat label="Belegt" value={`${formatNumber(usage.usedGb)} GB`} tone={usage.usedGb > usage.capacityGb ? "danger" : "active"} testId="res-used" />
                  <Stat label="Aktive Leases" value={r.active_leases.length} testId="res-active" />
                  <Stat label="Wartende Anfragen" value={r.waiting_requests.length} tone={r.waiting_requests.length ? "warning" : undefined} testId="res-waiting" />
                </div>
                <Meter value={usage.capacityGb > 0 ? usage.usedGb / usage.capacityGb : 0} label="Modell-Host-Speicher belegt" />
                {usage.byGroup.length ? (
                  <ul className="mini-list">
                    {usage.byGroup.map((g) => (
                      <li key={g.group}>
                        <strong>{g.group}</strong>: {formatNumber(g.usedGb)} GB ({g.leases.length} Lease{g.leases.length === 1 ? "" : "s"})
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted">Kein Modell belegt derzeit GPU-Speicher.</p>
                )}
              </Section>

              <Section title={`Aktive Leases (${String(r.active_leases.length)})`} id="res-leases">
                {r.active_leases.length ? (
                  <TableWrap label="Aktive Leases">
                    <table className="table" data-testid="leases-table">
                      <thead>
                        <tr>
                          <th scope="col">Ressource</th>
                          <th scope="col">Gruppe</th>
                          <th scope="col">Art</th>
                          <th scope="col">Halter</th>
                          <th scope="col">Job</th>
                          <th scope="col">Priorität</th>
                          <th scope="col">Zustand</th>
                          <th scope="col">Exklusiv</th>
                          <th scope="col">Verdrängbar</th>
                          <th scope="col">Gewicht</th>
                          <th scope="col">Heartbeat</th>
                          <th scope="col">Läuft ab</th>
                        </tr>
                      </thead>
                      <tbody>
                        {r.active_leases.map((l) => (
                          <tr key={l.id} data-testid="lease-row">
                            <th scope="row">
                              <code>{l.resource}</code>
                            </th>
                            <td>{l.resource_group}</td>
                            <td>{l.owner_kind}</td>
                            <td>{l.holder}</td>
                            <td>{l.owner_job_id ? <Link to={`/jobs/${l.owner_job_id}/resources`}>{shortId(l.owner_job_id)}</Link> : "–"}</td>
                            <td className="num">{l.priority}</td>
                            <td>
                              <Badge tone={l.state === "preempting" ? "warning" : "active"}>{resultLabel(l.state)}</Badge>
                            </td>
                            <td>{boolLabel(l.exclusive)}</td>
                            <td>{boolLabel(l.preemptible)}</td>
                            <td className="num">{l.weight}</td>
                            <td title={formatDateTime(l.heartbeat_at)}>{formatRelative(l.heartbeat_at, now)}</td>
                            <td>{formatDateTime(l.expires_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableWrap>
                ) : (
                  <Empty>Keine aktiven Leases.</Empty>
                )}
              </Section>

              <Section title={`Wartende Anfragen (${String(r.waiting_requests.length)})`} id="res-waiting">
                {r.waiting_requests.length ? (
                  <TableWrap label="Wartende Anfragen">
                    <table className="table" data-testid="waiting-table">
                      <thead>
                        <tr>
                          <th scope="col">Ressource</th>
                          <th scope="col">Art</th>
                          <th scope="col">Halter</th>
                          <th scope="col">Job</th>
                          <th scope="col">Priorität</th>
                          <th scope="col">Wartet seit</th>
                          <th scope="col">Läuft ab</th>
                        </tr>
                      </thead>
                      <tbody>
                        {r.waiting_requests.map((w) => (
                          <tr key={w.id} data-testid="waiting-row">
                            <th scope="row">
                              <code>{w.resource}</code>
                            </th>
                            <td>{w.owner_kind}</td>
                            <td>{w.holder}</td>
                            <td>{w.owner_job_id ? <Link to={`/jobs/${w.owner_job_id}/resources`}>{shortId(w.owner_job_id)}</Link> : "–"}</td>
                            <td className="num">{w.priority}</td>
                            <td title={formatDateTime(w.created_at)}>{formatRelative(w.created_at, now)}</td>
                            <td>{formatDateTime(w.expires_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableWrap>
                ) : (
                  <Empty>Keine wartenden Anfragen.</Empty>
                )}
              </Section>

              <Section title="Modellprofile und Ressourcengruppen" id="res-profiles">
                {profiles.length ? (
                  <TableWrap label="Modellprofile">
                    <table className="table">
                      <thead>
                        <tr>
                          <th scope="col">Alias</th>
                          <th scope="col">Rolle</th>
                          <th scope="col">Gruppe</th>
                          <th scope="col">Speicher</th>
                          <th scope="col">Exklusiv</th>
                          <th scope="col">Priorität</th>
                          <th scope="col">Host</th>
                        </tr>
                      </thead>
                      <tbody>
                        {profiles.map((p) => (
                          <tr key={p.alias}>
                            <th scope="row">{p.alias}</th>
                            <td>{p.role}</td>
                            <td>{p.resource_group ?? "–"}</td>
                            <td>{p.memory_gb !== undefined ? `${formatNumber(p.memory_gb)} GB` : "–"}</td>
                            <td>{boolLabel(p.exclusive)}</td>
                            <td className="num">{p.priority ?? "–"}</td>
                            <td>{p.host ?? "–"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableWrap>
                ) : (
                  <Empty>Keine Modellprofile geladen.</Empty>
                )}
              </Section>
            </div>
          );
        }}
      </AsyncBlock>
    </div>
  );
}
