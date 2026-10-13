/**
 * Plan helpers: turn plan versions + live steps into DAG nodes and summarise plan metadata.
 *
 * The current plan version is drawn from the live (non-superseded) steps, so status colours, correction steps
 * and runtime-added dependencies are always visible. Historical versions are drawn from their stored plan JSON.
 */
import type { PlanStepJson, PlanVersion, Step } from "../api/types";

export interface PlanDagNode {
  id: string;
  deps: string[];
  title: string;
  status: string | null;
  kind?: string;
  stepId?: string;
}

function planSteps(plan: PlanVersion | null | undefined): PlanStepJson[] {
  const steps = plan?.plan_json.steps;
  if (!Array.isArray(steps)) return [];
  return steps.filter((s): s is PlanStepJson => typeof s === "object" && typeof s.id === "string" && s.id.length > 0);
}

/** Nodes for one plan version. `isCurrent` selects the live step data instead of the stored plan JSON. */
export function planDagNodes(plan: PlanVersion | null | undefined, steps: readonly Step[], isCurrent: boolean): PlanDagNode[] {
  if (isCurrent || !plan) {
    const live = steps.filter((s) => !s.superseded);
    if (live.length) {
      return live.map((s) => ({
        id: s.step_key,
        deps: [...s.depends_on],
        title: s.title,
        status: s.status,
        kind: s.kind,
        stepId: s.id,
      }));
    }
  }
  return planSteps(plan).map((s) => ({
    id: s.id,
    deps: Array.isArray(s.depends_on) ? s.depends_on.filter((d): d is string => typeof d === "string") : [],
    title: s.title ?? s.id,
    status: null,
    kind: s.kind,
  }));
}

/** The plan version the job currently executes (falls back to the newest one). */
export function currentPlanVersion(versions: readonly PlanVersion[], current: number | null | undefined): PlanVersion | null {
  if (!versions.length) return null;
  if (current !== null && current !== undefined) {
    const hit = versions.find((v) => v.version === current);
    if (hit) return hit;
  }
  return versions.reduce((a, b) => (b.version > a.version ? b : a));
}

export const PLAN_SOURCE_LABELS: Record<string, string> = {
  planner: "Planer (Gemma)",
  replanner: "Neuplanung (Gemma)",
  fallback: "Fallback-Planer",
  manual: "Manuell",
};

export function planSourceLabel(source: string): string {
  return PLAN_SOURCE_LABELS[source] ?? source;
}

/** Status counts of non-superseded steps (`step_counts` of the API, recomputed for live updates). */
export function stepProgress(steps: readonly Step[]): { total: number; done: number; failed: number; active: number } {
  const live = steps.filter((s) => !s.superseded);
  const done = live.filter((s) => s.status === "completed").length;
  const failed = live.filter((s) => s.status === "failed").length;
  const active = live.filter((s) => ["leased", "running", "testing", "verifying", "reviewing", "checkpointed"].includes(s.status)).length;
  return { total: live.length, done, failed, active };
}
