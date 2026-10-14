import { useMemo } from "react";
import { api } from "../api/client";
import type { HermEvent } from "../api/types";
import { JobControls } from "../components/JobControls";
import { Tabs, type TabDef } from "../components/Tabs";
import { AsyncBlock, JobStatusBadge } from "../components/ui";
import { eventsWithPrefix, stepDataVersion } from "../lib/aggregate";
import { isErrorEvent, latestStatusLine, statusText } from "../lib/events";
import { formatTime, shortId } from "../lib/format";
import { Link, useRouter } from "../lib/router";
import { useApi, useThrottled } from "../lib/useApi";
import { useJobEvents } from "../lib/useEvents";
import { ApprovalsTab } from "./job/ApprovalsTab";
import { EventsTab, StreamBadge } from "./job/EventsTab";
import { ChangesTab, ReviewTab, TestsTab, VerifierTab } from "./job/EvidenceTabs";
import { ArtifactsTab, DeploymentTab, ErrorsTab, GitTab, MediaTab, RepositoryTab, ResourcesTab } from "./job/MiscTabs";
import { OverviewTab } from "./job/Overview";
import { PlanTab } from "./job/PlanTab";
import { ResearchTab } from "./job/ResearchTab";
import { StepsTab } from "./job/StepsTab";
import type { JobTabProps } from "./job/types";

const JOB_TABS: readonly { id: string; label: string }[] = [
  { id: "overview", label: "Übersicht" },
  { id: "plan", label: "Plan" },
  { id: "steps", label: "Schritte" },
  { id: "events", label: "Live-Events" },
  { id: "repository", label: "Repository" },
  { id: "changes", label: "Änderungen" },
  { id: "tests", label: "Tests" },
  { id: "verifier", label: "Verifier" },
  { id: "review", label: "Review" },
  { id: "research", label: "Research-Quellen" },
  { id: "resources", label: "Ressourcen" },
  { id: "artifacts", label: "Artefakte" },
  { id: "media", label: "Medien" },
  { id: "git", label: "Git" },
  { id: "deployment", label: "Deployment" },
  { id: "approvals", label: "Freigaben" },
  { id: "errors", label: "Fehler" },
];

/** Job-level events that change the job/step rows (status, counts, plan version). */
function jobDataVersion(events: readonly HermEvent[]): number {
  let n = 0;
  for (const e of events) {
    if (e.event_type.startsWith("job.") || e.event_type.startsWith("step.") || e.event_type.startsWith("replan.") || e.event_type === "planner.plan.created")
      n++;
  }
  return n;
}

function TabPanel({ tab, sub, props }: { tab: string; sub: string | null; props: JobTabProps }) {
  switch (tab) {
    case "plan":
      return <PlanTab {...props} />;
    case "steps":
      return <StepsTab {...props} sub={sub} />;
    case "events":
      return <EventsTab {...props} />;
    case "repository":
      return <RepositoryTab {...props} />;
    case "changes":
      return <ChangesTab {...props} />;
    case "tests":
      return <TestsTab {...props} />;
    case "verifier":
      return <VerifierTab {...props} />;
    case "review":
      return <ReviewTab {...props} />;
    case "research":
      return <ResearchTab {...props} />;
    case "resources":
      return <ResourcesTab {...props} />;
    case "artifacts":
      return <ArtifactsTab {...props} />;
    case "media":
      return <MediaTab {...props} />;
    case "git":
      return <GitTab {...props} />;
    case "deployment":
      return <DeploymentTab {...props} />;
    case "approvals":
      return <ApprovalsTab {...props} />;
    case "errors":
      return <ErrorsTab {...props} />;
    default:
      return <OverviewTab {...props} />;
  }
}

export function JobDetailPage({ jobId, tab, sub }: { jobId: string; tab: string; sub: string | null }) {
  const { navigate } = useRouter();
  const eventsState = useJobEvents(jobId);
  const { events } = eventsState;
  const jobVersion = useThrottled(jobDataVersion(events), 1000);
  const job = useApi(`job:${jobId}`, (s) => api.job(jobId, s), { pollMs: 15000, version: jobVersion });
  const stepVersion = stepDataVersion(events);
  const stepKeys = useMemo(() => new Map((job.data?.steps ?? []).map((s) => [s.id, s.step_key])), [job.data]);
  const activeTab = JOB_TABS.some((t) => t.id === tab) ? tab : "overview";
  const latest = latestStatusLine(events);
  const errorCount = events.filter(isErrorEvent).length;

  const tabs: TabDef[] = JOB_TABS.map((t) => {
    let count: number | null = null;
    if (t.id === "steps") count = job.data ? job.data.steps.filter((s) => !s.superseded).length : null;
    else if (t.id === "events") count = events.length;
    else if (t.id === "errors") count = errorCount || null;
    else if (t.id === "git") count = eventsWithPrefix(events, ["git"]).length || null;
    return { ...t, count };
  });

  return (
    <div className="page">
      <nav aria-label="Brotkrumen" className="breadcrumbs">
        <Link to="/jobs">Jobs</Link> <span aria-hidden="true">›</span> <span>{job.data?.title ?? shortId(jobId)}</span>
      </nav>
      <AsyncBlock result={job} loadingLabel="Job wird geladen …">
        {(j) => {
          const props: JobTabProps = { job: j, events, eventsState, stepKeys, stepVersion, reload: job.reload };
          return (
            <>
              <header className="job-head">
                <div className="job-title">
                  <h1 data-testid="job-title">{j.title}</h1>
                  <JobStatusBadge status={j.status} />
                  <StreamBadge stream={eventsState.stream} />
                </div>
                <p className="status-line" aria-live="polite" data-testid="status-line">
                  {latest ? (
                    <>
                      <span className="status-dot" aria-hidden="true" />
                      <strong>{statusText(latest)}</strong> <time dateTime={latest.ts}>{formatTime(latest.ts)}</time>
                    </>
                  ) : (
                    <span className="muted">Noch keine Statusmeldung.</span>
                  )}
                </p>
                <JobControls job={j} onDone={job.reload} />
              </header>
              <Tabs
                tabs={tabs}
                active={activeTab}
                label="Job-Ansichten"
                idPrefix="job"
                onChange={(id) => navigate(`/jobs/${jobId}${id === "overview" ? "" : `/${id}`}`, { replace: true })}
              />
              <div className="tab-panel" role="tabpanel" id="job-panel" aria-labelledby={`job-tab-${activeTab}`} data-testid={`panel-${activeTab}`}>
                <TabPanel tab={activeTab} sub={sub} props={props} />
              </div>
            </>
          );
        }}
      </AsyncBlock>
    </div>
  );
}
