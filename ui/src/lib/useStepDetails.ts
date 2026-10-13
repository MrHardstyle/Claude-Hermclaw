import { api } from "../api/client";
import type { Step, StepDetail } from "../api/types";
import { useApi, type ApiResult } from "./useApi";

/** Run `fn` over `items` with at most `limit` promises in flight; keeps input order. */
export async function mapLimit<T, R>(items: readonly T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out = new Array<R>(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i] as T);
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, worker));
  return out;
}

/**
 * Details (`GET /api/steps/{id}`) of all steps of a job – the data source of the Tests/Verifier/Review tabs.
 * `version` changes whenever step-level events arrive, which refetches in place.
 */
export function useStepDetails(jobId: string, steps: readonly Step[] | undefined, version: number): ApiResult<StepDetail[]> {
  const ids = steps ? steps.map((s) => s.id) : null;
  const key = ids ? `step-details:${jobId}:${ids.join(",")}` : null;
  return useApi(key, (signal) => mapLimit(ids ?? [], 6, (id) => api.step(id, signal)), { version });
}
