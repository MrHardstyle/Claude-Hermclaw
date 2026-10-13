import type { HermEvent, JobDetail } from "../../api/types";
import type { JobEventsResult } from "../../lib/useEvents";

/** Data every job tab receives from the job detail page (one job fetch + one event stream per page). */
export interface JobTabProps {
  job: JobDetail;
  events: HermEvent[];
  eventsState: JobEventsResult;
  /** step id → step key */
  stepKeys: Map<string, string>;
  /** increases with every step-level event (refetch trigger for step details) */
  stepVersion: number;
  reload: () => void;
}
