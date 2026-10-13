import { useId, useState } from "react";
import { api } from "../../api/client";
import { DagLegend, DagView } from "../../components/DagView";
import { AsyncBlock, Empty, JsonView, KeyValue, Section } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { currentPlanVersion, planDagNodes, planSourceLabel } from "../../lib/plan";
import { useRouter } from "../../lib/router";
import { useApi } from "../../lib/useApi";
import type { JobTabProps } from "./types";

export function PlanTab({ job }: JobTabProps) {
  const { navigate } = useRouter();
  const plans = useApi(`plan:${job.id}`, (s) => api.plan(job.id, s), { version: `${job.current_plan_version ?? ""}:${job.replan_count}` });
  const [chosen, setChosen] = useState<number | null>(null);
  const selectId = useId();

  return (
    <AsyncBlock result={plans}>
      {(versions) => {
        const current = currentPlanVersion(versions, job.current_plan_version);
        const selected = versions.find((v) => v.version === chosen) ?? current;
        const isCurrent = !selected || selected.version === current?.version;
        const nodes = planDagNodes(selected, job.steps, isCurrent);
        const p = selected?.plan_json;
        return (
          <div className="stack">
            <Section
              title="Plan"
              id="plan-main"
              actions={
                versions.length ? (
                  <div className="inline-field">
                    <label htmlFor={selectId}>Planversion</label>
                    <select
                      id={selectId}
                      value={selected?.version ?? ""}
                      onChange={(e) => setChosen(Number(e.target.value))}
                      data-testid="plan-version-select"
                    >
                      {[...versions]
                        .sort((a, b) => b.version - a.version)
                        .map((v) => (
                          <option key={v.id} value={v.version}>
                            v{v.version} · {planSourceLabel(v.source)}
                            {v.version === current?.version ? " (aktuell)" : ""}
                          </option>
                        ))}
                    </select>
                  </div>
                ) : null
              }
            >
              {selected ? (
                <>
                  <KeyValue
                    items={[
                      ["Version", `v${selected.version}${isCurrent ? " (aktuell)" : " (historisch)"}`],
                      ["Quelle", planSourceLabel(selected.source)],
                      ["Modell", selected.model_alias],
                      ["Erstellt", formatDateTime(selected.created_at)],
                      ["Reparaturversuche", String(selected.repair_attempts)],
                      ["Anlass", selected.reason],
                    ]}
                  />
                  {p?.goal ? (
                    <p>
                      <strong>Ziel:</strong> {p.goal}
                    </p>
                  ) : null}
                  {p?.summary ? <p className="prewrap">{p.summary}</p> : null}
                  {p?.assumptions?.length ? (
                    <>
                      <h3>Annahmen</h3>
                      <ul>
                        {p.assumptions.map((a, i) => (
                          <li key={i}>{a}</li>
                        ))}
                      </ul>
                    </>
                  ) : null}
                  {p?.risks?.length ? (
                    <>
                      <h3>Risiken</h3>
                      <ul>
                        {p.risks.map((a, i) => (
                          <li key={i}>{a}</li>
                        ))}
                      </ul>
                    </>
                  ) : null}
                  {p?.research_needed?.length ? (
                    <>
                      <h3>Research-Bedarf</h3>
                      <ul>
                        {p.research_needed.map((r, i) => (
                          <li key={i}>
                            {r.question}
                            {r.reason ? <span className="muted"> – {r.reason}</span> : null}
                          </li>
                        ))}
                      </ul>
                    </>
                  ) : null}
                  {selected.validation_errors.length ? (
                    <div className="warning-text">
                      <strong>Validierungsfehler:</strong>
                      <ul>
                        {selected.validation_errors.map((e, i) => (
                          <li key={i}>{typeof e === "string" ? e : JSON.stringify(e)}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </>
              ) : (
                <Empty>Noch kein Plan erstellt – der Planer (Gemma) plant den Job nach der Analyse.</Empty>
              )}
            </Section>
            <Section title="Abhängigkeitsgraph" id="plan-dag" actions={<DagLegend />}>
              <DagView
                nodes={nodes}
                label={`Plan-Graph Version ${selected ? String(selected.version) : "–"}`}
                onSelect={(key) => {
                  if (nodes.find((n) => n.id === key)?.stepId) navigate(`/jobs/${job.id}/steps/${encodeURIComponent(key)}`);
                }}
              />
              {selected ? <JsonView value={selected.plan_json as Record<string, unknown>} label="Plan (JSON)" /> : null}
            </Section>
          </div>
        );
      }}
    </AsyncBlock>
  );
}
