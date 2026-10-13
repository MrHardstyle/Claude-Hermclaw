import { useState } from "react";
import { api, ApiError, describeError } from "../api/client";
import type { JsonObject, WorkerDetail } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { AsyncBlock, Badge, Empty, ErrorBox, JsonView, KeyValue, Loading, Meter, Section, TableWrap, WorkerStateBadge } from "../components/ui";
import { formatDateTime, formatDuration, formatNumber, formatRelative } from "../lib/format";
import { WORKER_STATE_HINTS } from "../lib/labels";
import { Link, useRouter } from "../lib/router";
import { useApi, useNow } from "../lib/useApi";

function num(o: JsonObject, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "number" && Number.isFinite(v)) return v;
  }
  return null;
}

function str(o: JsonObject, ...keys: string[]): string | null {
  for (const k of keys) {
    const v = o[k];
    if (typeof v === "string" && v) return v;
  }
  return null;
}

/** GPU entry of a health sample (keys follow nvidia-smi / ROCm naming; unknown keys are shown as JSON). */
export function GpuCard({ gpu, index }: { gpu: JsonObject; index: number }) {
  const total = num(gpu, "memory_total_mb", "vram_total_mb", "memory_total");
  const used = num(gpu, "memory_used_mb", "vram_used_mb", "memory_used");
  const util = num(gpu, "utilization_percent", "utilization", "util_percent");
  const temp = num(gpu, "temperature_c", "temperature");
  return (
    <div className="gpu-card" data-testid="gpu-card">
      <strong>{str(gpu, "name", "model") ?? `GPU ${String(index)}`}</strong>
      {total !== null && used !== null ? (
        <p>
          VRAM {formatNumber(Math.round(used))} / {formatNumber(Math.round(total))} MB
          <Meter value={total > 0 ? used / total : 0} label={`VRAM GPU ${String(index)}`} />
        </p>
      ) : null}
      {util !== null ? <p>Auslastung {formatNumber(Math.round(util))} %</p> : null}
      {temp !== null ? <p>Temperatur {formatNumber(Math.round(temp))} °C</p> : null}
      <JsonView value={gpu} label="Rohdaten" />
    </div>
  );
}

/** Admin drain toggle: a drained worker finishes running work but receives no new assignments. */
function DrainControl({ d, onChanged }: { d: WorkerDetail; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const drained = d.admin_drain === true;
  const label = drained ? "Drain beenden" : "Drain aktivieren";
  return (
    <>
      <button
        type="button"
        className={`btn ${drained ? "" : "btn-danger-outline"}`}
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
        data-testid="worker-drain"
      >
        {label}
      </button>
      <ConfirmDialog
        open={open}
        title={`${label}?`}
        message={
          drained
            ? `Worker ${d.id} erhält wieder neue Aufgaben.`
            : `Worker ${d.id} erhält keine neuen Aufgaben mehr; laufende Arbeit wird nicht unterbrochen.`
        }
        confirmLabel={label}
        destructive={!drained}
        askReason
        busy={busy}
        error={error}
        onCancel={() => {
          setOpen(false);
          setError(null);
        }}
        onConfirm={(reason) => {
          setBusy(true);
          setError(null);
          api.drainWorker(d.id, !drained, reason).then(
            () => {
              setBusy(false);
              setOpen(false);
              onChanged();
            },
            (e: unknown) => {
              setBusy(false);
              setError(describeError(e));
            },
          );
        }}
      />
    </>
  );
}

function WorkerDetailView({ d, onChanged }: { d: WorkerDetail; onChanged: () => void }) {
  const h = d.health[0];
  return (
    <div data-testid="worker-detail">
      <div className="controls" role="group" aria-label="Worker-Steuerung">
        <DrainControl d={d} onChanged={onChanged} />
      </div>
      <KeyValue
        items={[
          ["Zustand", <WorkerStateBadge key="s" state={d.state} />],
          ["Grund", d.state_reason],
          ["Zustand seit", formatDateTime(d.state_changed_at)],
          ["Heartbeat-Alter", d.heartbeat_age_seconds !== null && d.heartbeat_age_seconds !== undefined ? formatDuration(d.heartbeat_age_seconds * 1000) : null],
          ["Kompatibel", d.compatible === false ? "Nein (Protokollversion)" : "Ja"],
          ["Wake-on-LAN", d.wol_enabled ? "aktiv" : "aus"],
          ["Drain", d.admin_drain ? "ja (keine neuen Aufgaben)" : "nein"],
          ["Dienste", d.service_versions ? Object.entries(d.service_versions).map(([k, v]) => `${k} ${v}`).join(", ") : null],
        ]}
      />
      {h ? (
        <>
          <h3>Letzte Messung ({formatDateTime(h.created_at)})</h3>
          <div className="stats-row">
            <div className="stat">
              <span className="stat-value">{formatNumber(Math.round(h.cpu_percent))} %</span>
              <span className="stat-label">CPU</span>
            </div>
            <div className="stat">
              <span className="stat-value">
                {formatNumber(Math.round(h.ram_used_mb / 1024))} / {formatNumber(Math.round(h.ram_total_mb / 1024))} GB
              </span>
              <span className="stat-label">RAM</span>
            </div>
            <div className="stat">
              <span className="stat-value">{formatNumber(Math.round(h.disk_free_mb / 1024))} GB</span>
              <span className="stat-label">Disk frei</span>
            </div>
            <div className="stat">
              <span className="stat-value">{formatDuration(h.uptime_seconds * 1000)}</span>
              <span className="stat-label">Laufzeit</span>
            </div>
          </div>
          {h.gpus.length ? (
            <div className="gpu-grid">
              {h.gpus.map((g, i) => (
                <GpuCard key={i} gpu={g} index={i} />
              ))}
            </div>
          ) : null}
          {h.loaded_models.length ? (
            <>
              <h4>Geladene Modelle</h4>
              <ul className="mini-list" data-testid="loaded-models">
                {h.loaded_models.map((m, i) => (
                  <li key={i}>
                    <code>{str(m, "name", "model") ?? JSON.stringify(m)}</code>
                    {num(m, "size_vram_mb", "vram_mb") !== null ? ` · ${formatNumber(num(m, "size_vram_mb", "vram_mb"))} MB VRAM` : ""}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </>
      ) : (
        <p className="muted">Keine Gesundheitsdaten (noch kein Heartbeat).</p>
      )}
    </div>
  );
}

export function Workers() {
  const { search, navigate } = useRouter();
  const selected = search.get("worker");
  const now = useNow(5000);
  const workers = useApi("workers", (s) => api.workers(s), { pollMs: 10000 });
  const detail = useApi(selected ? `worker:${selected}` : null, (s) => api.worker(selected ?? "", s), { pollMs: 10000 });

  return (
    <div className="page">
      <h1>Worker</h1>
      <Section title="Registrierte Worker" id="workers-list">
        <AsyncBlock result={workers}>
          {(ws) =>
            ws.length ? (
              <TableWrap label="Worker">
                <table className="table" data-testid="workers-table">
                  <thead>
                    <tr>
                      <th scope="col">Worker</th>
                      <th scope="col">Zustand</th>
                      <th scope="col">Art</th>
                      <th scope="col">Adresse</th>
                      <th scope="col">Version</th>
                      <th scope="col">Heartbeat</th>
                      <th scope="col">Aktiver Job</th>
                      <th scope="col">Fähigkeiten</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ws.map((w) => {
                      const state = w.display_state || w.state.toUpperCase();
                      return (
                        <tr key={w.id} className={selected === w.id ? "row-selected" : undefined} data-testid="worker-row" data-worker={w.id}>
                          <th scope="row">
                            <button
                              type="button"
                              className="link-button"
                              aria-pressed={selected === w.id}
                              onClick={() => navigate(selected === w.id ? "/workers" : `/workers?worker=${encodeURIComponent(w.id)}`, { replace: true })}
                            >
                              {w.id}
                            </button>
                            <span className="muted"> {w.hostname}</span>
                          </th>
                          <td>
                            <WorkerStateBadge state={state} />
                            <span className="sr-only"> – {WORKER_STATE_HINTS[state] ?? ""}</span>
                          </td>
                          <td>{w.kind}</td>
                          <td>
                            <code>{w.address}</code>
                          </td>
                          <td>
                            {w.worker_version ?? "–"}
                            {w.protocol_version !== null ? <span className="muted"> (Prot. {w.protocol_version})</span> : null}
                          </td>
                          <td title={formatDateTime(w.last_heartbeat_at)}>{formatRelative(w.last_heartbeat_at, now)}</td>
                          <td>{w.active_job_id ? <Link to={`/jobs/${w.active_job_id}`}>{w.active_job_id.slice(0, 8)}</Link> : "–"}</td>
                          <td>
                            {w.capabilities.map((c) => (
                              <Badge key={c} tone="neutral">
                                {c}
                              </Badge>
                            ))}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </TableWrap>
            ) : (
              <Empty>Keine Worker registriert. Worker melden sich per signiertem Heartbeat an.</Empty>
            )
          }
        </AsyncBlock>
      </Section>
      {selected ? (
        <Section title={`Worker ${selected}`} id="worker-detail">
          {detail.error instanceof ApiError && detail.error.status === 403 ? (
            <p className="warning-text">Die Detailansicht (Gesundheit, GPU, geladene Modelle) benötigt ein Admin-Token.</p>
          ) : detail.data ? (
            <WorkerDetailView
              d={detail.data}
              onChanged={() => {
                detail.reload();
                workers.reload();
              }}
            />
          ) : detail.error ? (
            <ErrorBox error={detail.error} onRetry={detail.reload} />
          ) : (
            <Loading />
          )}
        </Section>
      ) : (
        <p className="muted">Einen Worker anklicken, um Gesundheit, GPUs und geladene Modelle zu sehen.</p>
      )}
    </div>
  );
}
