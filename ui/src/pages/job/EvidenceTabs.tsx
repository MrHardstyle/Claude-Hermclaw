import { api } from "../../api/client";
import { DiffViewer } from "../../components/DiffViewer";
import { DownloadButton } from "../../components/Artifacts";
import { ReviewRunView, TestRunsTable, VerificationRunView } from "../../components/Evidence";
import { EventList } from "../../components/EventList";
import { AsyncBlock, Empty, Section, Stat } from "../../components/ui";
import { collectReviews, collectTests, collectVerifications, eventsWithPrefix } from "../../lib/aggregate";
import { isDiffTruncated, SERVER_DIFF_CHAR_LIMIT } from "../../lib/diff";
import { formatDateTime } from "../../lib/format";
import { useApi, useThrottled } from "../../lib/useApi";
import { useStepDetails } from "../../lib/useStepDetails";
import type { JobTabProps } from "./types";

function useDetails({ job, stepVersion }: JobTabProps) {
  return useStepDetails(job.id, job.steps, useThrottled(stepVersion, 1500));
}

export function TestsTab(props: JobTabProps) {
  const details = useDetails(props);
  const testEvents = eventsWithPrefix(props.events, ["test"]);
  return (
    <div className="stack">
      <Section title="Testläufe" id="tests-main">
        <AsyncBlock result={details}>
          {(ds) => {
            const { rows, totals } = collectTests(ds);
            return (
              <>
                <div className="stats-row" data-testid="tests-totals">
                  <Stat label="Testläufe" value={totals.runs} />
                  <Stat label="Läufe bestanden" value={totals.passedRuns} tone="success" />
                  <Stat label="Läufe fehlgeschlagen" value={totals.failedRuns} tone={totals.failedRuns ? "danger" : undefined} testId="tests-failed-runs" />
                  <Stat label="Tests bestanden" value={totals.passed} tone="success" testId="tests-passed" />
                  <Stat label="Tests fehlgeschlagen" value={totals.failed + totals.errors} tone={totals.failed + totals.errors ? "danger" : undefined} />
                  <Stat label="Übersprungen" value={totals.skipped} />
                </div>
                <TestRunsTable rows={rows.map((r) => ({ item: r.item, stepKey: r.stepKey }))} jobId={props.job.id} />
              </>
            );
          }}
        </AsyncBlock>
      </Section>
      {testEvents.length ? (
        <Section title="Test-Events" id="tests-events">
          <EventList events={testEvents} stepKeys={props.stepKeys} label="Test-Events" />
        </Section>
      ) : null}
    </div>
  );
}

export function VerifierTab(props: JobTabProps) {
  const details = useDetails(props);
  return (
    <Section title="Verifier" id="verifier-main">
      <AsyncBlock result={details}>
        {(ds) => {
          const { rows, totals } = collectVerifications(ds);
          return (
            <>
              <div className="stats-row" data-testid="verifier-totals">
                <Stat label="Verifikationen" value={totals.runs} />
                <Stat label="Bestanden" value={totals.passedRuns} tone="success" />
                <Stat label="Checks" value={totals.checks} testId="verifier-checks-total" />
                <Stat label="Checks fehlgeschlagen" value={totals.failedChecks} tone={totals.failedChecks ? "danger" : undefined} testId="verifier-checks-failed" />
                <Stat label="davon blockierend" value={totals.blockingFailed} tone={totals.blockingFailed ? "danger" : undefined} />
              </div>
              {rows.length ? (
                [...rows].reverse().map((r) => <VerificationRunView key={r.item.id} run={r.item} stepKey={r.stepKey} jobId={props.job.id} />)
              ) : (
                <Empty>Noch keine Verifikation durchgeführt.</Empty>
              )}
            </>
          );
        }}
      </AsyncBlock>
    </Section>
  );
}

export function ReviewTab(props: JobTabProps) {
  const details = useDetails(props);
  return (
    <Section title="Review" id="review-main">
      <AsyncBlock result={details}>
        {(ds) => {
          const { rows, findingsBySeverity, findings } = collectReviews(ds);
          return (
            <>
              <div className="stats-row" data-testid="review-totals">
                <Stat label="Reviews" value={rows.length} />
                <Stat label="Findings" value={findings} />
                <Stat label="Blocker" value={findingsBySeverity.blocker ?? 0} tone={findingsBySeverity.blocker ? "danger" : undefined} />
                <Stat label="Major" value={findingsBySeverity.major ?? 0} tone={findingsBySeverity.major ? "warning" : undefined} testId="review-major" />
                <Stat label="Minor" value={findingsBySeverity.minor ?? 0} />
              </div>
              {rows.length ? (
                [...rows].reverse().map((r) => <ReviewRunView key={r.item.id} run={r.item} stepKey={r.stepKey} jobId={props.job.id} />)
              ) : (
                <Empty>Noch kein Review durchgeführt.</Empty>
              )}
            </>
          );
        }}
      </AsyncBlock>
    </Section>
  );
}

export function ChangesTab({ job, events, stepKeys }: JobTabProps) {
  const changeEvents = eventsWithPrefix(events, ["file.changed", "git", "checkpoint"]);
  const version = useThrottled(changeEvents.length, 2000);
  const diff = useApi(`diff:${job.id}`, (s) => api.diff(job.id, s), { version });
  const fileEvents = eventsWithPrefix(events, ["file.changed"]);
  return (
    <div className="stack">
      <Section
        title="Änderungen"
        id="changes-main"
        actions={
          diff.data?.artifact_id ? <DownloadButton artifact={{ id: diff.data.artifact_id, name: `job-${job.id.slice(0, 8)}.diff` }} /> : null
        }
      >
        <AsyncBlock result={diff}>
          {(d) => (
            <>
              {d.created_at ? (
                <p className="muted">
                  Diff vom {formatDateTime(d.created_at)}
                  {d.step_id && stepKeys.get(d.step_id) ? ` (Schritt ${stepKeys.get(d.step_id) ?? ""})` : ""}
                </p>
              ) : null}
              {isDiffTruncated(d.diff, d.truncated) ? (
                <p className="warning-text diff-truncated" role="status" data-testid="diff-truncated">
                  Der Diff ist größer als {SERVER_DIFF_CHAR_LIMIT.toLocaleString("de-DE")} Zeichen und wird hier gekürzt angezeigt; die
                  letzte Datei kann unvollständig sein. Die vollständige Datei steht über „Herunterladen“ bereit.
                </p>
              ) : null}
              <DiffViewer diff={d.diff} />
            </>
          )}
        </AsyncBlock>
      </Section>
      {fileEvents.length ? (
        <Section title="Dateiänderungen (Events)" id="changes-events">
          <EventList events={fileEvents} stepKeys={stepKeys} label="Dateiänderungen" />
        </Section>
      ) : null}
    </div>
  );
}
