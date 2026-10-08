/**
 * Layered DAG layout (left → right) for plan/step graphs.
 *
 * 1. Layer = longest path from a root (dependencies always sit in an earlier column).
 * 2. Order inside a layer: barycenter of the dependencies' positions (a few sweeps), ties by id.
 * 3. Cycles or unknown dependencies never crash the view: cyclic nodes go into an extra last layer and
 *    are reported; unknown dependency ids are reported and dropped.
 */

export interface DagInputNode {
  id: string;
  deps: readonly string[];
}

export interface DagNodePos {
  id: string;
  layer: number;
  order: number;
  x: number;
  y: number;
}

export interface DagEdge {
  from: string;
  to: string;
  path: string;
}

export interface DagLayoutOptions {
  nodeWidth: number;
  nodeHeight: number;
  colGap: number;
  rowGap: number;
  padding: number;
}

export interface DagLayout {
  nodes: DagNodePos[];
  edges: DagEdge[];
  layers: string[][];
  width: number;
  height: number;
  cyclic: string[];
  missing: { node: string; dep: string }[];
}

export const DEFAULT_DAG_OPTIONS: DagLayoutOptions = { nodeWidth: 200, nodeHeight: 64, colGap: 72, rowGap: 22, padding: 16 };

export function assignLayers(nodes: readonly DagInputNode[]): {
  layerOf: Map<string, number>;
  cyclic: string[];
  missing: { node: string; dep: string }[];
} {
  const ids = new Set(nodes.map((n) => n.id));
  const deps = new Map<string, string[]>();
  const missing: { node: string; dep: string }[] = [];
  for (const n of nodes) {
    const valid: string[] = [];
    for (const d of new Set(n.deps)) {
      if (d === n.id) continue;
      if (ids.has(d)) valid.push(d);
      else missing.push({ node: n.id, dep: d });
    }
    deps.set(n.id, valid);
  }
  // Kahn's algorithm with longest-path layering
  const indeg = new Map<string, number>();
  const children = new Map<string, string[]>();
  for (const n of nodes) {
    indeg.set(n.id, (deps.get(n.id) ?? []).length);
    children.set(n.id, []);
  }
  for (const n of nodes) for (const d of deps.get(n.id) ?? []) children.get(d)?.push(n.id);
  const layerOf = new Map<string, number>();
  const queue = nodes.filter((n) => (indeg.get(n.id) ?? 0) === 0).map((n) => n.id);
  for (const id of queue) layerOf.set(id, 0);
  let head = 0;
  while (head < queue.length) {
    const cur = queue[head++] as string;
    const l = layerOf.get(cur) ?? 0;
    for (const c of children.get(cur) ?? []) {
      layerOf.set(c, Math.max(layerOf.get(c) ?? 0, l + 1));
      const left = (indeg.get(c) ?? 0) - 1;
      indeg.set(c, left);
      if (left === 0) queue.push(c);
    }
  }
  const cyclic = nodes.filter((n) => !queue.includes(n.id)).map((n) => n.id);
  if (cyclic.length) {
    const maxLayer = Math.max(-1, ...[...layerOf.values()]);
    for (const id of cyclic) layerOf.set(id, maxLayer + 1);
  }
  return { layerOf, cyclic, missing };
}

/** Cubic bezier from the right edge of the source to the left edge of the target. */
export function edgePath(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.max(24, (x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

export function layoutDag(nodes: readonly DagInputNode[], options: Partial<DagLayoutOptions> = {}): DagLayout {
  const o: DagLayoutOptions = { ...DEFAULT_DAG_OPTIONS, ...options };
  const { layerOf, cyclic, missing } = assignLayers(nodes);
  const depCount = Math.max(0, ...[...layerOf.values()]) + 1;
  const layers: string[][] = Array.from({ length: nodes.length ? depCount : 0 }, () => []);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  for (const n of [...nodes].sort((a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }))) {
    layers[layerOf.get(n.id) ?? 0]?.push(n.id);
  }
  // barycenter ordering, a few downward sweeps
  const pos = new Map<string, number>();
  const refresh = () => {
    layers.forEach((layer) => {
      layer.forEach((id, i) => pos.set(id, i));
    });
  };
  refresh();
  for (let sweep = 0; sweep < 3; sweep++) {
    for (let li = 1; li < layers.length; li++) {
      const layer = layers[li] ?? [];
      const bary = new Map<string, number>();
      for (const id of layer) {
        const ds = (byId.get(id)?.deps ?? []).filter((d) => pos.has(d) && (layerOf.get(d) ?? 0) < li);
        bary.set(id, ds.length ? ds.reduce((s, d) => s + (pos.get(d) ?? 0), 0) / ds.length : (pos.get(id) ?? 0));
      }
      layer.sort((a, b) => {
        const diff = (bary.get(a) ?? 0) - (bary.get(b) ?? 0);
        return diff !== 0 ? diff : a.localeCompare(b, undefined, { numeric: true });
      });
      layer.forEach((id, i) => pos.set(id, i));
    }
  }
  const maxRows = Math.max(0, ...layers.map((l) => l.length));
  const height = nodes.length ? o.padding * 2 + maxRows * o.nodeHeight + (maxRows - 1) * o.rowGap : 0;
  const width = nodes.length ? o.padding * 2 + layers.length * o.nodeWidth + (layers.length - 1) * o.colGap : 0;
  const placed: DagNodePos[] = [];
  const at = new Map<string, DagNodePos>();
  layers.forEach((layer, li) => {
    const colHeight = layer.length * o.nodeHeight + (layer.length - 1) * o.rowGap;
    const offset = (height - o.padding * 2 - colHeight) / 2; // vertically centre short columns
    layer.forEach((id, i) => {
      const p: DagNodePos = {
        id,
        layer: li,
        order: i,
        x: o.padding + li * (o.nodeWidth + o.colGap),
        y: o.padding + offset + i * (o.nodeHeight + o.rowGap),
      };
      placed.push(p);
      at.set(id, p);
    });
  });
  const edges: DagEdge[] = [];
  for (const n of nodes) {
    const to = at.get(n.id);
    if (!to) continue;
    for (const d of new Set(n.deps)) {
      const from = at.get(d);
      if (!from || d === n.id) continue;
      edges.push({
        from: d,
        to: n.id,
        path: edgePath(from.x + o.nodeWidth, from.y + o.nodeHeight / 2, to.x, to.y + o.nodeHeight / 2),
      });
    }
  }
  return { nodes: placed, edges, layers, width, height, cyclic, missing };
}
