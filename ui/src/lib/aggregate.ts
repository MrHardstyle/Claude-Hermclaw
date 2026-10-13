/**
 * Pure aggregation helpers for the job tabs (tests, verifier, review, research, resources).
 * Kept free of React so they can be unit tested.
 */
import type {
  HermEvent,
  ModelProfile,
  ResearchClaim,
  ResearchRun,
  ResearchSource,
  ResourceLease,
  ReviewFinding,
  ReviewRun,
  StepDetail,
  TestRun,
  VerificationCheck,
  VerificationRun,
} from "../api/types";

export interface WithStep<T> {
  stepKey: string;
  stepTitle: string;
  stepId: string;
  item: T;
}

function flatten<T>(details: readonly StepDetail[], pick: (d: StepDetail) => readonly T[]): WithStep<T>[] {
  const out: WithStep<T>[] = [];
  for (const d of details) {
    for (const item of pick(d)) out.push({ stepKey: d.step.step_key, stepTitle: d.step.title, stepId: d.step.id, item });
  }
  return out;
}

function byCreated<T extends { item: { created_at: string } }>(rows: T[]): T[] {
  return rows.sort((a, b) => a.item.created_at.localeCompare(b.item.created_at));
}

// ---------------------------------------------------------------- tests
export interface TestTotals {
  runs: number;
  passedRuns: number;
  failedRuns: number;
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
}

export function collectTests(details: readonly StepDetail[]): { rows: WithStep<TestRun>[]; totals: TestTotals } {
  const rows = byCreated(flatten(details, (d) => d.tests));
  const totals: TestTotals = { runs: rows.length, passedRuns: 0, failedRuns: 0, passed: 0, failed: 0, errors: 0, skipped: 0 };
  for (const { item } of rows) {
    if (item.status === "passed") totals.passedRuns++;
    else if (["failed", "error", "timeout"].includes(item.status)) totals.failedRuns++;
    totals.passed += item.passed;
    totals.failed += item.failed;
    totals.errors += item.errors;
    totals.skipped += item.skipped;
  }
  return { rows, totals };
}

// ---------------------------------------------------------------- verifier
export interface VerifierTotals {
  runs: number;
  passedRuns: number;
  checks: number;
  failedChecks: number;
  blockingFailed: number;
}

export function isFailedCheck(c: Pick<VerificationCheck, "status">): boolean {
  return ["failed", "error", "fail"].includes(c.status);
}

export function collectVerifications(details: readonly StepDetail[]): { rows: WithStep<VerificationRun>[]; totals: VerifierTotals } {
  const rows = byCreated(flatten(details, (d) => d.verifications));
  const totals: VerifierTotals = { runs: rows.length, passedRuns: 0, checks: 0, failedChecks: 0, blockingFailed: 0 };
  for (const { item } of rows) {
    if (item.passed) totals.passedRuns++;
    for (const c of item.checks) {
      totals.checks++;
      if (isFailedCheck(c)) {
        totals.failedChecks++;
        if (c.blocking) totals.blockingFailed++;
      }
    }
  }
  return { rows, totals };
}

/** Failed blocking checks first, then failed, then the rest (stable by name). */
export function sortChecks(checks: readonly VerificationCheck[]): VerificationCheck[] {
  const rank = (c: VerificationCheck) => (isFailedCheck(c) ? (c.blocking ? 0 : 1) : c.status === "skip" || c.status === "skipped" ? 3 : 2);
  return [...checks].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}

// ---------------------------------------------------------------- review
export const FINDING_SEVERITY_ORDER = ["blocker", "major", "minor"] as const;

export function severityRank(sev: string): number {
  const i = (FINDING_SEVERITY_ORDER as readonly string[]).indexOf(sev.toLowerCase());
  return i < 0 ? FINDING_SEVERITY_ORDER.length : i;
}

export function sortFindings(findings: readonly ReviewFinding[]): ReviewFinding[] {
  return [...findings].sort((a, b) => severityRank(a.severity) - severityRank(b.severity) || (a.path ?? "").localeCompare(b.path ?? ""));
}

export function collectReviews(details: readonly StepDetail[]): {
  rows: WithStep<ReviewRun>[];
  findingsBySeverity: Record<string, number>;
  findings: number;
} {
  const rows = byCreated(flatten(details, (d) => d.reviews));
  const findingsBySeverity: Record<string, number> = {};
  let findings = 0;
  for (const { item } of rows) {
    for (const f of item.findings) {
      const k = f.severity.toLowerCase();
      findingsBySeverity[k] = (findingsBySeverity[k] ?? 0) + 1;
      findings++;
    }
  }
  return { rows, findingsBySeverity, findings };
}

// ---------------------------------------------------------------- research
export interface ClaimView {
  claim: ResearchClaim;
  /** linked sources with their 1-based citation number in the run */
  sources: { source: ResearchSource; n: number }[];
  missingSourceIds: string[];
}

export interface ResearchRunView {
  run: ResearchRun;
  sources: { source: ResearchSource; n: number }[];
  claims: ClaimView[];
  /** claims grouped by `contradiction_group` (only groups with at least one claim) */
  contradictionGroups: { group: number; claims: ClaimView[] }[];
}

/** Only http(s) URLs are rendered as links (no javascript:/data: URLs from fetched content). */
export function safeExternalUrl(url: string): string | null {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

export function researchRunView(run: ResearchRun): ResearchRunView {
  const sources = [...run.sources]
    .sort((a, b) => b.authority_score - a.authority_score || b.relevance_score - a.relevance_score || a.url.localeCompare(b.url))
    .map((source, i) => ({ source, n: i + 1 }));
  const byId = new Map(sources.map((s) => [s.source.id, s]));
  const claims: ClaimView[] = run.claims.map((claim) => {
    const linked: { source: ResearchSource; n: number }[] = [];
    const missing: string[] = [];
    for (const id of claim.source_ids) {
      const s = byId.get(id);
      if (s) linked.push(s);
      else missing.push(id);
    }
    linked.sort((a, b) => a.n - b.n);
    return { claim, sources: linked, missingSourceIds: missing };
  });
  const groups = new Map<number, ClaimView[]>();
  for (const c of claims) {
    const g = c.claim.contradiction_group;
    if (g === null) continue;
    const list = groups.get(g) ?? [];
    list.push(c);
    groups.set(g, list);
  }
  const contradictionGroups = [...groups.entries()].sort((a, b) => a[0] - b[0]).map(([group, cs]) => ({ group, claims: cs }));
  return { run, sources, claims, contradictionGroups };
}

export function researchTotals(runs: readonly ResearchRun[]): { runs: number; sources: number; claims: number; contradictions: number } {
  let sources = 0;
  let claims = 0;
  let contradictions = 0;
  for (const r of runs) {
    sources += r.sources.length;
    claims += r.claims.length;
    contradictions += new Set(r.claims.map((c) => c.contradiction_group).filter((g) => g !== null)).size;
    if (!r.claims.some((c) => c.contradiction_group !== null)) contradictions += r.contradictions.length;
  }
  return { runs: runs.length, sources, claims, contradictions };
}

// ---------------------------------------------------------------- resources / GPU
export interface GpuUsage {
  capacityGb: number;
  usedGb: number;
  leases: ResourceLease[];
  byGroup: { group: string; usedGb: number; leases: ResourceLease[] }[];
}

/** Model-host memory in use = sum of the weights of holding leases on model resource groups (weight = memory_gb). */
export function gpuUsage(leases: readonly ResourceLease[], profiles: readonly ModelProfile[], capacityGb: number): GpuUsage {
  const groups = new Set(profiles.map((p) => p.resource_group).filter((g): g is string => typeof g === "string" && g.length > 0));
  const holding = leases.filter((l) => (l.state === "active" || l.state === "preempting") && groups.has(l.resource_group));
  const byGroupMap = new Map<string, ResourceLease[]>();
  for (const l of holding) {
    const list = byGroupMap.get(l.resource_group) ?? [];
    list.push(l);
    byGroupMap.set(l.resource_group, list);
  }
  const byGroup = [...byGroupMap.entries()]
    .map(([group, ls]) => ({ group, usedGb: ls.reduce((s, l) => s + (Number.isFinite(l.weight) ? l.weight : 0), 0), leases: ls }))
    .sort((a, b) => a.group.localeCompare(b.group));
  return { capacityGb, usedGb: byGroup.reduce((s, g) => s + g.usedGb, 0), leases: holding, byGroup };
}

export function leasesForJob<T extends { owner_job_id: string | null }>(rows: readonly T[], jobId: string): T[] {
  return rows.filter((r) => r.owner_job_id === jobId);
}

// ---------------------------------------------------------------- event groups
export function eventsWithPrefix(events: readonly HermEvent[], prefixes: readonly string[]): HermEvent[] {
  return events.filter((e) => prefixes.some((p) => e.event_type === p || e.event_type.startsWith(`${p}.`)));
}

/** Monotone counter of events that change step-level data; used to refetch step details live. */
export function stepDataVersion(events: readonly HermEvent[]): number {
  let n = 0;
  for (const e of events) {
    const t = e.event_type;
    if (
      t.startsWith("attempt.") ||
      t.startsWith("test.") ||
      t.startsWith("verifier.") ||
      t.startsWith("review.") ||
      t.startsWith("scope.") ||
      t.startsWith("tool.call.") ||
      t === "command.run" ||
      t === "step.transition" ||
      t === "step.created"
    )
      n++;
  }
  return n;
}

// ---------------------------------------------------------------- acceptance criteria
const ACCEPTANCE_TYPE_LABELS: Record<string, string> = {
  presence: "Vorhanden",
  absence: "Nicht vorhanden",
  command: "Befehl",
  test: "Test",
  diff: "Diff",
  scope: "Scope",
  schema: "Schema",
  security: "Sicherheit",
  artifact: "Artefakt",
};

/** One-line German description of an acceptance criterion (hermclaw/contracts/acceptance.py). */
export function describeAcceptance(c: unknown): { type: string; text: string } {
  if (typeof c === "string") return { type: "Kriterium", text: c };
  if (typeof c !== "object" || c === null || Array.isArray(c)) return { type: "Kriterium", text: JSON.stringify(c) };
  const o = c as Record<string, unknown>;
  const type = typeof o.type === "string" ? o.type : "kriterium";
  const parts: string[] = [];
  if (typeof o.description === "string" && o.description) parts.push(o.description);
  for (const k of ["path_glob", "path", "command", "pattern"]) {
    const v = o[k];
    if (typeof v === "string" && v) parts.push(`${k}: ${v}`);
  }
  for (const k of ["must_change", "must_not_change"]) {
    const v = o[k];
    if (Array.isArray(v) && v.length) parts.push(`${k}: ${v.map(String).join(", ")}`);
  }
  return { type: ACCEPTANCE_TYPE_LABELS[type] ?? type, text: parts.join(" · ") || JSON.stringify(o) };
}
