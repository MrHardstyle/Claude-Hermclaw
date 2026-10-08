import { useMemo, type KeyboardEvent } from "react";
import { layoutDag, type DagInputNode } from "../lib/dag";
import { stepStatusLabel, stepTone } from "../lib/labels";
import { truncate } from "../lib/format";

export interface DagNodeData extends DagInputNode {
  title: string;
  status: string | null;
  kind?: string;
}

const NODE_W = 210;
const NODE_H = 66;

export function DagView({
  nodes,
  onSelect,
  selected,
  label,
}: {
  nodes: DagNodeData[];
  onSelect?: (id: string) => void;
  selected?: string | null;
  label: string;
}) {
  const layout = useMemo(() => layoutDag(nodes, { nodeWidth: NODE_W, nodeHeight: NODE_H }), [nodes]);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const onKey = (e: KeyboardEvent<SVGGElement>, id: string) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect?.(id);
    }
  };
  if (!nodes.length) return <p className="empty">Keine Schritte im Plan.</p>;
  return (
    <div className="dag">
      {layout.cyclic.length ? (
        <p className="warning-text" role="alert">
          Zyklische Abhängigkeiten erkannt: {layout.cyclic.join(", ")}
        </p>
      ) : null}
      {layout.missing.length ? (
        <p className="warning-text">
          Unbekannte Abhängigkeiten: {layout.missing.map((m) => `${m.node} → ${m.dep}`).join(", ")}
        </p>
      ) : null}
      <div className="dag-scroll">
        <svg
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          role="group"
          aria-label={label}
          data-testid="dag"
        >
          <defs>
            <marker id="dag-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" className="dag-arrow" />
            </marker>
          </defs>
          <g className="dag-edges">
            {layout.edges.map((e) => (
              <path
                key={`${e.from}->${e.to}`}
                d={e.path}
                className="dag-edge"
                markerEnd="url(#dag-arrow)"
                data-testid="dag-edge"
                data-from={e.from}
                data-to={e.to}
              />
            ))}
          </g>
          <g className="dag-nodes">
            {layout.nodes.map((p) => {
              const n = byId.get(p.id);
              if (!n) return null;
              const tone = n.status ? stepTone(n.status) : "neutral";
              const statusLabel = n.status ? stepStatusLabel(n.status) : "geplant";
              return (
                <g
                  key={p.id}
                  transform={`translate(${p.x},${p.y})`}
                  className={`dag-node tone-${tone} ${selected === p.id ? "is-selected" : ""}`}
                  data-testid="dag-node"
                  data-step={p.id}
                  data-status={n.status ?? "planned"}
                  role="button"
                  tabIndex={0}
                  aria-label={`${p.id} ${n.title}, Status: ${statusLabel}`}
                  aria-pressed={selected === p.id}
                  onClick={() => onSelect?.(p.id)}
                  onKeyDown={(e) => {
                    onKey(e, p.id);
                  }}
                >
                  <title>{`${p.id} – ${n.title} (${statusLabel})`}</title>
                  <rect width={NODE_W} height={NODE_H} rx={8} className="dag-rect" />
                  <rect width={6} height={NODE_H} rx={3} className="dag-stripe" />
                  <text x={14} y={22} className="dag-key">
                    {p.id}
                    {n.kind ? ` · ${n.kind}` : ""}
                  </text>
                  <text x={14} y={41} className="dag-title">
                    {truncate(n.title, 28)}
                  </text>
                  <text x={14} y={58} className="dag-status">
                    {statusLabel}
                  </text>
                </g>
              );
            })}
          </g>
        </svg>
      </div>
    </div>
  );
}

export function DagLegend() {
  const items = ["pending", "ready", "running", "verifying", "completed", "failed", "blocked", "cancelled"];
  return (
    <ul className="dag-legend" aria-label="Legende Schrittstatus">
      {items.map((s) => (
        <li key={s}>
          <span className={`legend-swatch tone-${stepTone(s)}`} aria-hidden="true" /> {stepStatusLabel(s)}
        </li>
      ))}
    </ul>
  );
}
