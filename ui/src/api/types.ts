/**
 * Types mirroring the FastAPI responses (docs/contracts/openapi.json, hermclaw/api/schemas.py).
 * Endpoints that return untyped dicts (`/api/steps/{id}`, `/api/workers`, `/api/resources`, …) are typed
 * after the ORM columns they serialise (hermclaw/persistence/models.py).
 */

export type Uuid = string;
export type IsoDate = string;
export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type JsonObject = Record<string, Json>;

export const JOB_STATUSES = [
  "queued",
  "inventory",
  "discovering",
  "researching",
  "planning",
  "waiting_for_resources",
  "waiting_for_worker",
  "waking_worker",
  "running",
  "testing",
  "verifying",
  "reviewing",
  "correcting",
  "replanning",
  "waiting_for_user",
  "blocked",
  "committing",
  "deploying",
  "succeeded",
  "failed",
  "cancelled",
] as const;
export type JobStatus = (typeof JOB_STATUSES)[number];

export const STEP_STATUSES = [
  "pending",
  "ready",
  "leased",
  "running",
  "checkpointed",
  "testing",
  "verifying",
  "reviewing",
  "completed",
  "failed",
  "blocked",
  "cancelled",
] as const;
export type StepStatus = (typeof STEP_STATUSES)[number];

export const WORKER_STATES = ["offline", "starting", "ready", "busy", "draining", "sleeping", "waking", "error"] as const;
export type WorkerState = (typeof WORKER_STATES)[number];

export type Severity = "debug" | "info" | "warning" | "error" | "critical";

export interface Step {
  id: Uuid;
  step_key: string;
  title: string;
  kind: string;
  capability: string;
  goal: string;
  status: string;
  risk: string;
  attempt_count: number;
  correction_count: number;
  superseded: boolean;
  assigned_worker_id: string | null;
  current_scope_version: number | null;
  depends_on: string[];
  acceptance: Json[];
  error_code: string | null;
  error_message: string | null;
  started_at: IsoDate | null;
  finished_at: IsoDate | null;
  result: JsonObject;
}

export interface Job {
  id: Uuid;
  title: string;
  prompt: string;
  status: string;
  priority: number;
  repository_id: Uuid | null;
  base_branch: string | null;
  current_plan_version: number | null;
  replan_count: number;
  cancel_requested: boolean;
  pause_requested: boolean;
  result_summary: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: IsoDate;
  updated_at: IsoDate;
  started_at: IsoDate | null;
  finished_at: IsoDate | null;
  metadata: JsonObject;
}

export interface JobDetail extends Job {
  steps: Step[];
  step_counts: Record<string, number>;
}

export interface JobList {
  items: Job[];
  total: number;
}

export interface RepositoryRef {
  name?: string;
  url?: string;
  base_branch?: string;
}

export interface JobCreateBody {
  prompt: string;
  title?: string;
  repository?: RepositoryRef;
  constraints: string[];
  priority: number;
  allow_network_research: boolean;
  auto_commit: boolean;
  create_merge_request?: boolean | null;
}

/** Normalised event (REST `EventOut` uses `ts`, the SSE `EventEnvelope` uses `timestamp`). */
export interface HermEvent {
  sequence: number;
  event_id: Uuid;
  ts: IsoDate;
  job_id: Uuid | null;
  step_id: Uuid | null;
  attempt_id: Uuid | null;
  source_type: string;
  source_id: string | null;
  event_type: string;
  severity: string;
  payload: JsonObject;
  correlation_id: string | null;
  duration_ms: number | null;
}

export type ControlAction = "cancel" | "pause" | "resume" | "retry" | "replan";

export interface ControlResult {
  job_id: Uuid;
  action: ControlAction;
  status: string;
  accepted: boolean;
  detail: string;
}

export interface Artifact {
  id: Uuid;
  job_id: Uuid | null;
  step_id: Uuid | null;
  kind: string;
  name: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  created_at: IsoDate;
}

export interface PlanStepJson {
  id: string;
  title?: string;
  kind?: string;
  capability?: string;
  goal?: string;
  depends_on?: string[];
  risk?: string;
}

export interface PlanJson {
  goal?: string;
  summary?: string;
  assumptions?: string[];
  risks?: string[];
  steps?: PlanStepJson[];
  research_needed?: { question: string; reason?: string }[];
}

export interface PlanVersion {
  id: Uuid;
  version: number;
  source: string;
  model_alias: string | null;
  plan_json: PlanJson;
  validation_errors: Json[];
  repair_attempts: number;
  reason: string | null;
  created_at: IsoDate;
}

export interface DiffResponse {
  diff: string;
  artifact_id: Uuid | null;
  step_id?: Uuid | null;
  created_at?: IsoDate;
}

// ---------------------------------------------------------------- step detail (/api/steps/{id})
export interface StepAttempt {
  id: Uuid;
  step_id: Uuid;
  job_id: Uuid;
  attempt_no: number;
  kind: string;
  status: string;
  worker_id: string | null;
  model_alias: string | null;
  turns_used: number;
  outcome: string | null;
  error_code: string | null;
  summary: string | null;
  started_at: IsoDate;
  finished_at: IsoDate | null;
}

export interface ScopeVersion {
  id: Uuid;
  step_id: Uuid;
  version: number;
  status: string;
  contract: JsonObject;
  evidence: JsonObject;
  reason: string | null;
  created_at: IsoDate;
}

export interface ToolCall {
  id: Uuid;
  attempt_id: Uuid | null;
  turn: number;
  tool: string;
  arguments: JsonObject;
  status: string;
  result_summary: string | null;
  error_code: string | null;
  started_at: IsoDate;
  duration_ms: number | null;
}

export interface CommandRun {
  id: Uuid;
  attempt_id: Uuid | null;
  worker_id: string | null;
  target: string;
  command: string;
  classification: string;
  cwd: string | null;
  exit_code: number | null;
  timed_out: boolean;
  network: boolean;
  stdout_excerpt: string | null;
  stderr_excerpt: string | null;
  duration_ms: number | null;
  created_at: IsoDate;
}

export interface TestRun {
  id: Uuid;
  step_id: Uuid | null;
  attempt_id: Uuid | null;
  command: string;
  framework: string;
  status: string;
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
  output_excerpt: string | null;
  duration_ms: number | null;
  created_at: IsoDate;
}

export interface VerificationCheck {
  id: Uuid;
  verification_run_id: Uuid;
  check_type: string;
  name: string;
  status: string;
  blocking: boolean;
  message: string | null;
  evidence: JsonObject;
}

export interface VerificationRun {
  id: Uuid;
  step_id: Uuid;
  attempt_id: Uuid | null;
  passed: boolean;
  status: string;
  summary: string | null;
  changed_files: Json[];
  created_at: IsoDate;
  finished_at: IsoDate | null;
  checks: VerificationCheck[];
}

export interface ReviewFinding {
  id: Uuid;
  review_run_id: Uuid;
  severity: string;
  path: string | null;
  summary: string;
  evidence: string | null;
  suggested_fix: string | null;
}

export interface ReviewRun {
  id: Uuid;
  step_id: Uuid;
  attempt_id: Uuid | null;
  model_alias: string | null;
  status: string;
  verdict: string | null;
  raw_verdict: string | null;
  invariant_override: boolean;
  summary: string | null;
  created_at: IsoDate;
  finished_at: IsoDate | null;
  findings: ReviewFinding[];
}

export interface StepDetail {
  step: Step;
  attempts: StepAttempt[];
  scope_versions: ScopeVersion[];
  tool_calls: ToolCall[];
  commands: CommandRun[];
  tests: TestRun[];
  verifications: VerificationRun[];
  reviews: ReviewRun[];
}

// ---------------------------------------------------------------- research (/api/jobs/{id}/research)
export interface ResearchSource {
  id: Uuid;
  research_run_id: Uuid;
  title: string;
  url: string;
  domain: string;
  retrieved_at: IsoDate;
  published_at: IsoDate | null;
  source_type: string;
  authority_score: number;
  relevance_score: number;
  content_hash: string;
  excerpt: string | null;
  status: string;
  error: string | null;
}

export interface ResearchClaim {
  id: Uuid;
  research_run_id: Uuid;
  claim: string;
  confidence: number;
  used_for_decision: boolean;
  decision_ref: string | null;
  contradiction_group: number | null;
  created_at: IsoDate;
  source_ids: Uuid[];
}

export interface ResearchRun {
  id: Uuid;
  job_id: Uuid | null;
  step_id: Uuid | null;
  question: string;
  status: string;
  queries: Json[];
  synthesis: string | null;
  contradictions: Json[];
  model_alias: string | null;
  created_at: IsoDate;
  finished_at: IsoDate | null;
  sources: ResearchSource[];
  claims: ResearchClaim[];
}

// ---------------------------------------------------------------- system
export interface Worker {
  id: string;
  hostname: string;
  address: string;
  kind: string;
  state: string;
  display_state: string;
  api_url: string | null;
  worker_version: string | null;
  protocol_version: number | null;
  last_heartbeat_at: IsoDate | null;
  active_job_id: string | null;
  active_step_id: string | null;
  wol: JsonObject;
  metadata: JsonObject;
  capabilities: string[];
  created_at: IsoDate;
  updated_at: IsoDate;
}

export interface WorkerHealthSample {
  created_at: IsoDate;
  state: string;
  cpu_percent: number;
  ram_total_mb: number;
  ram_used_mb: number;
  disk_free_mb: number;
  gpus: JsonObject[];
  loaded_models: JsonObject[];
  active_job: string | null;
  active_step: string | null;
  uptime_seconds: number;
}

export interface WorkerDetail {
  id: string;
  hostname: string;
  state: string;
  state_reason?: string | null;
  heartbeat_age_seconds?: number | null;
  admin_drain?: boolean;
  wol_enabled?: boolean;
  service_versions?: Record<string, string>;
  declared_capabilities?: string[];
  health: WorkerHealthSample[];
}

export interface ModelStats {
  calls: number;
  avg_latency_ms?: number;
  completion_tokens?: number;
  errors?: number;
}

export interface ModelProfile {
  alias: string;
  role: string;
  model: string;
  kind?: string;
  host_worker_id?: string | null;
  context_tokens?: number;
  max_output_tokens?: number;
  resource_group?: string;
  exclusive?: boolean;
  priority?: number;
  memory_gb?: number;
  enabled?: boolean;
  persisted: boolean;
  stats: ModelStats;
}

export interface ModelsResponse {
  litellm: { base_url: string };
  profiles: ModelProfile[];
}

export interface ResourceLease {
  id: Uuid;
  resource: string;
  resource_group: string;
  owner_job_id: Uuid | null;
  owner_step_id: Uuid | null;
  owner_kind: string;
  holder: string;
  priority: number;
  state: string;
  preemptible: boolean;
  exclusive: boolean;
  weight: number;
  acquired_at: IsoDate;
  heartbeat_at: IsoDate;
  expires_at: IsoDate;
  released_at: IsoDate | null;
  release_reason: string | null;
  metadata: JsonObject;
}

export interface ResourceRequest {
  id: Uuid;
  resource: string;
  owner_job_id: Uuid | null;
  owner_step_id: Uuid | null;
  owner_kind: string;
  holder: string;
  priority: number;
  state: string;
  expires_at: IsoDate;
  created_at: IsoDate;
}

export interface ResourcesResponse {
  active_leases: ResourceLease[];
  waiting_requests: ResourceRequest[];
  model_host_capacity_gb: number;
}

export interface Repository {
  id: Uuid;
  name: string;
  url: string;
  default_branch: string;
  provider: string;
  gitlab_project_id: string | null;
  protected_branches: string[];
  metadata: JsonObject;
}

export interface BugRecord {
  id: Uuid;
  bug_key: string;
  title: string;
  severity: string;
  component: string;
  phase: string | null;
  status: string;
  blocking: boolean;
  job_id: Uuid | null;
  reproduction: string | null;
  expected: string | null;
  actual: string | null;
  workaround: string | null;
  regression_test: string | null;
  fix_commit: string | null;
  created_at: IsoDate;
  updated_at: IsoDate;
}

export interface StatsResponse {
  jobs_by_status: Record<string, number>;
  jobs_total: number;
}

export interface VersionResponse {
  version: string;
  protocol_version: number;
  env: string;
  instance: string;
}
