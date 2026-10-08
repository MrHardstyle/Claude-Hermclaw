import { describe, expect, it } from "vitest";
import { assignLayers, edgePath, layoutDag } from "../../src/lib/dag";

describe("assignLayers", () => {
  it("uses longest-path layering", () => {
    const { layerOf, cyclic, missing } = assignLayers([
      { id: "S001", deps: [] },
      { id: "S002", deps: ["S001"] },
      { id: "S003", deps: ["S001"] },
      { id: "S004", deps: ["S002", "S003"] },
      { id: "S005", deps: ["S001", "S004"] },
    ]);
    expect(Object.fromEntries(layerOf)).toEqual({ S001: 0, S002: 1, S003: 1, S004: 2, S005: 3 });
    expect(cyclic).toEqual([]);
    expect(missing).toEqual([]);
  });

  it("reports cycles and unknown dependencies without throwing", () => {
    const { layerOf, cyclic, missing } = assignLayers([
      { id: "A", deps: [] },
      { id: "B", deps: ["C"] },
      { id: "C", deps: ["B"] },
      { id: "D", deps: ["X", "D"] },
    ]);
    expect(cyclic.sort()).toEqual(["B", "C"]);
    expect(missing).toEqual([{ node: "D", dep: "X" }]);
    expect(layerOf.get("B")).toBe(1);
    expect(layerOf.get("D")).toBe(0);
  });
});

describe("layoutDag", () => {
  const nodes = [
    { id: "S001", deps: [] },
    { id: "S002", deps: ["S001"] },
    { id: "S003", deps: ["S001"] },
    { id: "S004", deps: ["S003"] },
  ];

  it("places dependencies left of dependants and creates one edge per dependency", () => {
    const l = layoutDag(nodes, { nodeWidth: 100, nodeHeight: 40, colGap: 50, rowGap: 10, padding: 5 });
    const pos = Object.fromEntries(l.nodes.map((n) => [n.id, n]));
    expect(l.layers).toEqual([["S001"], ["S002", "S003"], ["S004"]]);
    expect(pos.S001?.x).toBe(5);
    expect(pos.S002?.x).toBe(155);
    expect(pos.S004?.x).toBe(305);
    expect(l.edges.map((e) => `${e.from}->${e.to}`).sort()).toEqual(["S001->S002", "S001->S003", "S003->S004"]);
    expect(l.width).toBe(5 * 2 + 3 * 100 + 2 * 50);
    expect(l.height).toBe(5 * 2 + 2 * 40 + 10);
    // no overlap within a column
    expect(Math.abs((pos.S002?.y ?? 0) - (pos.S003?.y ?? 0))).toBeGreaterThanOrEqual(40);
  });

  it("orders by barycenter to reduce crossings", () => {
    const l = layoutDag([
      { id: "A", deps: [] },
      { id: "B", deps: [] },
      { id: "C", deps: ["B"] },
      { id: "D", deps: ["A"] },
    ]);
    expect(l.layers[1]).toEqual(["D", "C"]);
  });

  it("returns an empty layout for no nodes", () => {
    const l = layoutDag([]);
    expect(l.nodes).toEqual([]);
    expect(l.width).toBe(0);
  });

  it("builds bezier paths", () => {
    expect(edgePath(0, 10, 100, 50)).toBe("M 0 10 C 50 10, 50 50, 100 50");
  });
});
