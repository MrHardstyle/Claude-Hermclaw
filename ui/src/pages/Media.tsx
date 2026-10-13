import { useId } from "react";
import { api } from "../api/client";
import { EventList } from "../components/EventList";
import { MediaGallery } from "../components/MediaGallery";
import { AsyncBlock, Empty, Section } from "../components/ui";
import { formatDateTime } from "../lib/format";
import { jobStatusLabel } from "../lib/labels";
import { Link, useRouter } from "../lib/router";
import { useApi } from "../lib/useApi";

const MEDIA_EVENT_TYPES = "media.started,media.finished";

export function Media() {
  const { search, navigate } = useRouter();
  const jobId = search.get("job");
  const selectId = useId();
  const jobs = useApi("media-jobs", (s) => api.jobs({ limit: 100 }, s), { pollMs: 30000 });
  const events = useApi(jobId ? `media-events:${jobId}` : null, (s) => api.events(jobId ?? "", { types: MEDIA_EVENT_TYPES, limit: 500 }, s), {
    pollMs: 10000,
  });

  return (
    <div className="page">
      <h1>Medien</h1>
      <Section
        title="Bilder und Videos"
        id="media-page"
        actions={
          <div className="inline-field">
            <label htmlFor={selectId}>Job</label>
            <select
              id={selectId}
              value={jobId ?? ""}
              onChange={(e) => navigate(e.target.value ? `/media?job=${encodeURIComponent(e.target.value)}` : "/media", { replace: true })}
              data-testid="media-job-select"
            >
              <option value="">– Job wählen –</option>
              {(jobs.data?.items ?? []).map((j) => (
                <option key={j.id} value={j.id}>
                  {j.title} ({jobStatusLabel(j.status)}, {formatDateTime(j.created_at)})
                </option>
              ))}
            </select>
          </div>
        }
      >
        {jobId ? (
          <>
            <p>
              <Link to={`/jobs/${jobId}/media`}>Zum Job</Link>
            </p>
            <MediaGallery jobId={jobId} version={events.data?.length} />
          </>
        ) : (
          <Empty>Einen Job wählen, um seine Bilder und Videos anzusehen.</Empty>
        )}
      </Section>
      {jobId ? (
        <Section title="Medien-Events" id="media-page-events">
          <AsyncBlock result={events}>
            {(evs) => (evs.length ? <EventList events={evs} label="Medien-Events" /> : <Empty>Keine Medien-Events.</Empty>)}
          </AsyncBlock>
        </Section>
      ) : null}
    </div>
  );
}
